"""
Executor：确定性执行引擎（不依赖 LLM）。

职责：
- 校验 MonitoringPlan（安全、必需字段、可执行性）
- 生成/落盘“提交脚本”和“登录节点监控脚本”
- 提交作业、解析 jobid、启动监控
- 收集失败信息（stdout/stderr/返回码），用于回传给 Fixer
"""

import os
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from perfbench.agent.plan_utils import detect_command_paths, render_cmd_template
from perfbench.agent.schema import ErrorBundle, ExecCommandResult, MonitoringPlan
from perfbench.utils.logger import get_logger
from perfbench.utils.script_parser import parse_slurm_script

logger = get_logger()


_FORBIDDEN_TOKENS = [
    # 注入内容禁止破坏性行为（尽量保守）
    "rm ",
    "rm\t",
    "rm\n",
    "scancel",
    "bkill",
    "qdel",
    "kill ",
    "kill\t",
    "sudo",
    "curl ",
    "curl\t",
    "wget ",
    "wget\t",
    "ssh ",
    "ssh\t",
    "scp ",
    "scp\t",
]


def _looks_safe_injection(text: str) -> bool:
    lowered = text.lower()
    for t in _FORBIDDEN_TOKENS:
        if t in lowered:
            return False
    return True


def _looks_like_monitor_script(text: str) -> bool:
    """
    粗略检查监控脚本是否符合 PerfBench 预期（主要为了兼容后续报告解析）。
    - 必须出现 sacct 以及 sacct_$ts.log（或等价字符串）
    """

    lowered = (text or "").lower()
    if "sacct" not in lowered:
        return False
    if "sacct_" not in lowered or ".log" not in lowered:
        return False
    return True


def create_job_dir(output_path: str) -> str:
    """
    在 output_path 下创建 perfbench_时间戳 目录。
    """

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_dir = os.path.join(output_path, "perfbench_{ts}".format(ts=timestamp))
    os.makedirs(job_dir, exist_ok=True)
    return job_dir


def _inject_text_into_slurm_script(original: str, injection_text: str, anchor: str) -> str:
    """
    将 injection_text 注入到 Slurm 脚本中。

    anchor：
    - after_last_sbatch：插入到最后一个 #SBATCH 之后（推荐）
    - after_shebang：插入到 shebang 后一行
    - start_of_file：插入到文件开头
    """

    lines = original.splitlines(keepends=True)
    if not lines:
        lines = ["#!/bin/bash\n"]

    # 确保存在 shebang（与原实现保持一致）
    if not lines[0].startswith("#!"):
        lines.insert(0, "#!/bin/bash\n")

    if injection_text and not injection_text.endswith("\n"):
        injection_text = injection_text + "\n"

    insert_pos = 0
    if anchor == "start_of_file":
        insert_pos = 0
    elif anchor == "after_shebang":
        insert_pos = 1
    else:  # after_last_sbatch
        last_sbatch_idx = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("#SBATCH"):
                last_sbatch_idx = i
        insert_pos = (last_sbatch_idx + 1) if last_sbatch_idx != -1 else 1

    lines.insert(insert_pos, injection_text)
    return "".join(lines)


def _write_text(path: str, content: str, *, chmod: Optional[int] = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if chmod is not None:
        try:
            os.chmod(path, chmod)
        except Exception:
            # Windows 下可能无效；不阻断流程
            pass


def _run_cmd(cmd: List[str], *, cwd: Optional[str] = None) -> ExecCommandResult:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return ExecCommandResult(
            cmd=cmd,
            cwd=cwd,
            returncode=p.returncode,
            stdout=p.stdout,
            stderr=p.stderr,
        )
    except Exception as e:
        return ExecCommandResult(cmd=cmd, cwd=cwd, returncode=None, stdout=None, stderr=str(e))


def _parse_jobid(output: str, jobid_regex: str) -> Optional[str]:
    try:
        m = re.search(jobid_regex, output or "")
        if m:
            return m.group(1)
    except re.error:
        return None
    return None


def _generate_slurm_monitor_script(
    *,
    jobid: str,
    interval_sec: int,
    outdir: str,
    collectors: List[str],
    end_states: List[str],
    include_squeue_check: bool,
    run_seff_on_end: bool,
    available_commands: Dict[str, Optional[str]],
) -> str:
    """
    生成登录节点监控 bash 脚本（内容由 Executor 确定性生成）。
    """

    # 允许集合二次校验（避免 LLM 乱写）
    allowed_collectors = {"sacct", "sinfo", "sstat", "scontrol"}
    collectors = [c for c in collectors if c in allowed_collectors]

    # 对缺失命令做降级
    collectors = [c for c in collectors if available_commands.get(c)]

    include_squeue_check = bool(include_squeue_check and available_commands.get("squeue"))
    run_seff_on_end = bool(run_seff_on_end and available_commands.get("seff"))

    # 结束状态合并为 bash 正则：COMPLETED|FAILED|...
    state_re = "|".join([re.escape(s) for s in end_states]) if end_states else "COMPLETED|FAILED|CANCELLED|TIMEOUT"

    lines = []
    lines.append("#!/bin/bash\n")
    lines.append("# PerfBench login-node monitoring (agent)\n")
    lines.append("JOBID={jobid}\n".format(jobid=jobid))
    lines.append("INTERVAL={itv}\n".format(itv=int(interval_sec)))
    lines.append("OUTDIR=\"{outdir}\"\n".format(outdir=outdir))
    lines.append("\n")
    lines.append("mkdir -p \"$OUTDIR\"\n")
    lines.append("\n")
    lines.append("while true; do\n")
    lines.append("    ts=$(date +%Y%m%d_%H%M%S)\n")

    if "sacct" in collectors:
        lines.append(
            "    sacct -j $JOBID --format=JobID,JobName%20,State,Elapsed,MaxRSS,AllocCPUs -P "
            "> \"$OUTDIR/sacct_$ts.log\" 2>&1\n"
        )
    if "sinfo" in collectors:
        lines.append("    sinfo -N -o \"%N %t %f\" > \"$OUTDIR/sinfo_$ts.log\" 2>&1 || true\n")
    if "sstat" in collectors:
        lines.append(
            "    sstat -j $JOBID --format=JobID,MaxRSS,AveRSS,MaxVMSize -P "
            "> \"$OUTDIR/sstat_$ts.log\" 2>&1 || true\n"
        )
    if "scontrol" in collectors:
        lines.append("    scontrol show job $JOBID > \"$OUTDIR/scontrol_$ts.log\" 2>&1 || true\n")

    lines.append("\n")
    # 结束条件：sacct state 命中 或 squeue 为空（可选）
    lines.append("    state=$(sacct -j $JOBID -n -o State -P 2>/dev/null | head -n1)\n")
    if include_squeue_check:
        lines.append("    inqueue=$(squeue -j $JOBID -h 2>/dev/null | wc -l)\n")
        lines.append("    if [[ \"$state\" =~ ({re}) || $inqueue -eq 0 ]]; then\n".format(re=state_re))
    else:
        lines.append("    if [[ \"$state\" =~ ({re}) ]]; then\n".format(re=state_re))

    if run_seff_on_end:
        lines.append("        seff $JOBID > \"$OUTDIR/seff_$ts.log\" 2>&1 || true\n")
    lines.append("        echo \"Job $JOBID finished with state $state at $ts\" > \"$OUTDIR/job_end_$ts.log\"\n")
    lines.append("        break\n")
    lines.append("    fi\n")
    lines.append("\n")
    lines.append("    sleep $INTERVAL\n")
    lines.append("done\n")
    return "".join(lines)


def _render_monitor_script_text(raw: str, *, jobid: str, interval_sec: int, job_dir: str) -> str:
    """
    渲染 LLM 提供的监控脚本（只做安全的字符串替换，不用 format，避免与 bash 的 ${VAR} 冲突）。
    """

    text = raw or ""
    text = text.replace("{jobid}", str(jobid))
    text = text.replace("{interval_sec}", str(int(interval_sec)))
    text = text.replace("{job_dir}", str(job_dir))

    if not text.lstrip().startswith("#!"):
        text = "#!/bin/bash\n" + text

    # 在 shebang 后注入标准变量（便于统一 OUTDIR/INTERVAL/JOBID 语义）
    lines = text.splitlines(keepends=True)
    if lines:
        inject = "JOBID={jobid}\nINTERVAL={itv}\nOUTDIR=\"{outdir}\"\n".format(
            jobid=str(jobid), itv=int(interval_sec), outdir=str(job_dir)
        )
        lines.insert(1, inject)
        text = "".join(lines)

    return text


def execute_plan_slurm(
    *,
    plan_dict: Dict[str, Any],
    script_path: str,
    interval_sec: int,
    job_dir: str,
    available_commands: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[ErrorBundle]]:
    """
    执行 MonitoringPlan（Slurm）。

    返回：
    - success
    - jobid
    - script_info
    - error_bundle（失败时）
    """

    try:
        plan = MonitoringPlan.model_validate(plan_dict)
    except Exception as e:
        return False, None, None, ErrorBundle(step="plan", message="计划校验失败: {e}".format(e=str(e)), previous_plan=plan_dict)

    if plan.scheduler != "slurm":
        return False, None, None, ErrorBundle(step="plan", message="当前仅支持 slurm 调度器", previous_plan=plan_dict)

    # interval 强一致：以 CLI 入参为准（避免 LLM 乱改）
    if plan.monitor.interval_sec != int(interval_sec):
        plan.monitor.interval_sec = int(interval_sec)

    if available_commands is None:
        available_commands = detect_command_paths(
            ["sbatch", "sacct", "squeue", "sinfo", "sstat", "scontrol", "seff"]
        )

    # 1) 解析脚本
    script_info = parse_slurm_script(script_path)
    if not script_info:
        return (
            False,
            None,
            None,
            ErrorBundle(step="inject", message="解析 Slurm 脚本失败", previous_plan=plan_dict, available_commands=available_commands),
        )

    # 2) 注入并写脚本
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            original = f.read()
    except Exception as e:
        return (
            False,
            None,
            None,
            ErrorBundle(
                step="inject",
                message="读取脚本失败: {e}".format(e=str(e)),
                previous_plan=plan_dict,
                available_commands=available_commands,
            ),
        )

    injection_text = plan.job_script.injection_text or ""
    injection_text = injection_text.replace("{job_dir}", job_dir)
    if injection_text and not _looks_safe_injection(injection_text):
        return (
            False,
            None,
            None,
            ErrorBundle(
                step="inject",
                message="注入文本包含疑似危险内容，已拒绝执行",
                previous_plan=plan_dict,
                available_commands=available_commands,
            ),
        )

    modified = _inject_text_into_slurm_script(original, injection_text, plan.job_script.injection_anchor)

    script_dir = os.path.dirname(os.path.abspath(script_path))
    submit_script_path = os.path.join(script_dir, plan.job_script.output_basename)
    audit_script_path = os.path.join(job_dir, plan.job_script.job_dir_copy_basename)
    try:
        _write_text(audit_script_path, modified, chmod=0o755)
        _write_text(submit_script_path, modified, chmod=0o755)
    except Exception as e:
        return (
            False,
            None,
            None,
            ErrorBundle(
                step="inject",
                message="写入脚本失败: {e}".format(e=str(e)),
                previous_plan=plan_dict,
                available_commands=available_commands,
            ),
        )

    # 3) 提交作业
    submit_cmd = render_cmd_template(plan.submit.cmd, {"script_basename": plan.job_script.output_basename})
    submit_cwd = script_dir if plan.submit.cwd == "script_dir" else None
    submit_res = _run_cmd(submit_cmd, cwd=submit_cwd)
    combined_out = (submit_res.stdout or "") + "\n" + (submit_res.stderr or "")
    # returncode=None 表示命令执行阶段异常，也视为失败
    if submit_res.returncode != 0:
        return (
            False,
            None,
            script_info,
            ErrorBundle(
                step="submit",
                message="作业提交失败（sbatch 返回非 0）",
                command=submit_res,
                previous_plan=plan_dict,
                available_commands=available_commands,
            ),
        )

    jobid = _parse_jobid(combined_out, plan.submit.jobid_regex)
    if not jobid:
        return (
            False,
            None,
            script_info,
            ErrorBundle(
                step="submit",
                message="无法从提交输出中解析 jobid",
                command=submit_res,
                previous_plan=plan_dict,
                available_commands=available_commands,
            ),
        )

    # 4) 生成并启动监控脚本
    try:
        if plan.monitor.script_text:
            monitor_script = _render_monitor_script_text(
                plan.monitor.script_text,
                jobid=jobid,
                interval_sec=plan.monitor.interval_sec,
                job_dir=job_dir,
            )
            if not _looks_safe_injection(monitor_script):
                raise RuntimeError("监控脚本包含疑似危险内容，已拒绝执行")
            if not _looks_like_monitor_script(monitor_script):
                raise RuntimeError("监控脚本不符合预期（缺少 sacct 日志输出约束）")
        else:
            monitor_script = _generate_slurm_monitor_script(
                jobid=jobid,
                interval_sec=plan.monitor.interval_sec,
                outdir=job_dir,
                collectors=list(plan.monitor.collectors),
                end_states=list(plan.monitor.end_states),
                include_squeue_check=plan.monitor.include_squeue_check,
                run_seff_on_end=plan.monitor.run_seff_on_end,
                available_commands=available_commands,
            )
        monitor_sh = os.path.join(job_dir, plan.monitor.script_basename)
        monitor_pid = os.path.join(job_dir, plan.monitor.pid_basename)
        _write_text(monitor_sh, monitor_script, chmod=0o755)

        p = subprocess.Popen([monitor_sh], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _write_text(monitor_pid, str(p.pid), chmod=None)
    except Exception as e:
        return (
            False,
            jobid,
            script_info,
            ErrorBundle(
                step="monitor",
                message="启动登录节点监控失败: {e}".format(e=str(e)),
                previous_plan=plan_dict,
                available_commands=available_commands,
                artifacts_excerpt={"monitor_script": (monitor_script[:2000] if "monitor_script" in locals() else "")},
            ),
        )

    logger.info("Agent 执行完成：jobid={jobid}，输出目录={job_dir}".format(jobid=jobid, job_dir=job_dir))
    return True, jobid, script_info, None
