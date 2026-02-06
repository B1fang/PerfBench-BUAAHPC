"""
LangGraph 工作流：Coder → Executor → Fixer（闭环）。

当前仅支持 Slurm。
"""

import json
import os
from typing import Any, Dict, Optional, Tuple, TypedDict

from perfbench.agent.executor import create_job_dir, execute_plan_slurm
from perfbench.agent.llm import DeepSeekConfig, create_deepseek_chat_llm
from perfbench.agent.plan_utils import apply_dotpath_changes, detect_command_paths, extract_json_object
from perfbench.agent.prompts import build_coder_prompt, build_fixer_prompt
from perfbench.agent.schema import FixSuggestion, MonitoringPlan
from perfbench.utils.logger import get_logger
from perfbench.utils.script_parser import parse_slurm_script

logger = get_logger()


class AgentState(TypedDict, total=False):
    round_idx: int
    plan: Optional[Dict[str, Any]]
    fix_suggestion: Optional[Dict[str, Any]]
    exec_success: bool
    jobid: Optional[str]
    error_bundle: Optional[Dict[str, Any]]


def _import_langchain_messages():  # pragma: no cover
    """
    兼容不同版本的 LangChain messages 位置。
    """

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception:  # pragma: no cover
        from langchain.schema import HumanMessage, SystemMessage  # type: ignore
    return SystemMessage, HumanMessage


def _import_langgraph():  # pragma: no cover
    try:
        from langgraph.graph import END, StateGraph
    except Exception as e:  # pragma: no cover
        raise RuntimeError("未安装依赖：请安装 langgraph，例如：pip install langgraph") from e
    return StateGraph, END


def _safe_write(path: str, content: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _truncate_for_prompt(text: str, max_chars: int = 12000) -> str:
    """
    适度截断脚本文本，避免 prompt 过长导致模型失败。
    """

    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.6)]
    tail = text[-int(max_chars * 0.4) :]
    return head + "\n\n# ...（中间内容已截断）...\n\n" + tail


def _llm_invoke_json(llm, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
    SystemMessage, HumanMessage = _import_langchain_messages()
    resp = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    text = resp.content if hasattr(resp, "content") else str(resp)
    return extract_json_object(text)


def run_slurm_agent_workflow(
    *,
    script_path: str,
    interval_sec: int,
    output_path: str,
    deepseek_cfg: DeepSeekConfig,
    max_fix_rounds: int = 2,
) -> Tuple[str, Dict[str, Any]]:
    """
    运行 Slurm agent 工作流。

    返回：
    - job_dir
    - script_info（来自 parse_slurm_script）
    """

    # 预处理：job_dir、脚本解析、命令探测（这些不交给 LLM）
    job_dir = create_job_dir(output_path)
    script_info = parse_slurm_script(script_path)
    if not script_info:
        raise RuntimeError("解析 Slurm 脚本失败，无法进入 agent 流程")

    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()
    except Exception as e:
        raise RuntimeError("读取脚本失败: {e}".format(e=str(e))) from e

    script_content_for_llm = _truncate_for_prompt(script_content)

    available_commands = detect_command_paths(["sbatch", "sacct", "squeue", "sinfo", "sstat", "scontrol", "seff"])

    llm = create_deepseek_chat_llm(deepseek_cfg)

    StateGraph, END = _import_langgraph()

    graph = StateGraph(AgentState)

    def coder_node(state: Dict[str, Any]) -> Dict[str, Any]:
        prev_plan = state.get("plan")
        fix_sug = state.get("fix_suggestion")
        if prev_plan and isinstance(fix_sug, dict):
            changes = fix_sug.get("changes")
            if isinstance(changes, dict) and changes:
                try:
                    prev_plan = apply_dotpath_changes(prev_plan, changes)
                except Exception:
                    pass
        system_p, user_p = build_coder_prompt(
            script_content=script_content_for_llm,
            script_info=script_info,
            job_dir=job_dir,
            interval_sec=int(interval_sec),
            available_commands=available_commands,
            previous_plan=prev_plan,
            fix_suggestion=fix_sug,
        )

        plan_obj = _llm_invoke_json(llm, system_p, user_p)
        # 结构校验：尽早失败，避免执行危险/无效 plan
        plan = MonitoringPlan.model_validate(plan_obj).model_dump()

        # 记录审计产物（不影响主流程）
        round_idx = int(state.get("round_idx", 0))
        _safe_write(os.path.join(job_dir, "agent_trace", "coder_plan_{i}.json".format(i=round_idx)), json.dumps(plan, ensure_ascii=False, indent=2))

        state["plan"] = plan
        state["fix_suggestion"] = None
        return state

    def executor_node(state: Dict[str, Any]) -> Dict[str, Any]:
        plan = state.get("plan") or {}
        ok, jobid, _script_info, err = execute_plan_slurm(
            plan_dict=plan,
            script_path=script_path,
            interval_sec=int(interval_sec),
            job_dir=job_dir,
            available_commands=available_commands,
        )
        state["exec_success"] = bool(ok)
        state["jobid"] = jobid
        state["error_bundle"] = err.model_dump() if err else None
        return state

    def fixer_node(state: Dict[str, Any]) -> Dict[str, Any]:
        err = state.get("error_bundle") or {}
        system_p, user_p = build_fixer_prompt(error_bundle=err)
        sug_obj = _llm_invoke_json(llm, system_p, user_p)
        sug = FixSuggestion.model_validate(sug_obj).model_dump()

        # 记录审计产物
        round_idx = int(state.get("round_idx", 0))
        _safe_write(os.path.join(job_dir, "agent_trace", "fix_suggestion_{i}.json".format(i=round_idx)), json.dumps(sug, ensure_ascii=False, indent=2))

        # 将修复建议也落到 state，供下一轮 coder 参考
        state["fix_suggestion"] = sug

        # 增加轮次计数（最多 max_fix_rounds 次修复）
        state["round_idx"] = round_idx + 1
        return state

    def decide_next(state: Dict[str, Any]) -> str:
        if state.get("exec_success"):
            return "end"
        round_idx = int(state.get("round_idx", 0))
        if round_idx >= int(max_fix_rounds):
            return "end"
        return "fixer"

    graph.add_node("coder", coder_node)
    graph.add_node("executor", executor_node)
    graph.add_node("fixer", fixer_node)

    graph.set_entry_point("coder")
    graph.add_edge("coder", "executor")
    graph.add_conditional_edges("executor", decide_next, {"fixer": "fixer", "end": END})
    graph.add_edge("fixer", "coder")

    app = graph.compile()
    final_state: Dict[str, Any] = app.invoke({"round_idx": 0, "plan": None, "fix_suggestion": None})

    if not final_state.get("exec_success"):
        err = final_state.get("error_bundle")
        if err is not None:
            _safe_write(
                os.path.join(job_dir, "agent_trace", "error_bundle.json"),
                json.dumps(err, ensure_ascii=False, indent=2),
            )
        raise RuntimeError("Agent 执行失败，错误包: {err}".format(err=json.dumps(err, ensure_ascii=False)))

    return job_dir, script_info
