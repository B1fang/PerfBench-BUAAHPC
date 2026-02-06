"""
Agent 工作流的结构化 Schema 定义。

约束目标：
- LLM 只负责输出结构化 Plan（JSON），不直接执行命令。
- Executor 负责校验/落盘/执行/采集/回滚，确保可控与可复现。

说明：
- 目前只支持 Slurm，因此 scheduler 固定为 "slurm"。
- 未来扩展其它调度器时，建议保持该 Schema 向后兼容（新增字段而非破坏性修改）。
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


SchedulerType = Literal["slurm"]
InjectionAnchor = Literal["after_last_sbatch", "after_shebang", "start_of_file"]


class JobScriptPlan(BaseModel):
    """
    作业脚本生成/注入计划（Slurm）。

    约定：
    - Executor 不就地修改用户原脚本，只会在原脚本目录生成一个提交用脚本（默认 run.slurm）。
    - 同时会在 job_dir 里落盘一份 modified_script.slurm 作为审计产物。
    """

    output_basename: str = Field(default="run.slurm", description="提交用脚本文件名（写到原脚本所在目录）")
    job_dir_copy_basename: str = Field(default="modified_script.slurm", description="审计用脚本文件名（写到 job_dir）")
    injection_anchor: InjectionAnchor = Field(default="after_last_sbatch", description="注入锚点")
    injection_text: str = Field(default="", description="注入到脚本中的文本（建议以换行结尾）")


class SubmitPlan(BaseModel):
    """
    作业提交计划（Slurm）。
    """

    cmd: List[str] = Field(default_factory=lambda: ["sbatch", "{script_basename}"], description="提交命令模板")
    jobid_regex: str = Field(default=r"Submitted batch job (\d+)", description="从提交输出中提取 jobid 的正则")
    cwd: Literal["script_dir"] = Field(default="script_dir", description="提交命令的工作目录（目前固定脚本目录）")


class MonitorPlan(BaseModel):
    """
    登录节点监控计划（Slurm）。

    约束：
    - collectors 只能从允许集合中选择（Executor 会做二次校验和降级）。
    - 输出文件命名要与现有解析逻辑兼容（如 sacct_YYYYMMDD_HHMMSS.log）。
    """

    script_basename: str = Field(default="monitor_login.sh", description="监控脚本文件名（写到 job_dir）")
    pid_basename: str = Field(default="monitor_login.pid", description="监控脚本 pid 文件名（写到 job_dir）")
    interval_sec: int = Field(..., ge=1, description="采集间隔（秒）")
    script_text: Optional[str] = Field(
        default=None,
        description=(
            "监控脚本内容（bash）。若提供则优先使用该内容；否则由 Executor 生成默认脚本。"
            "可使用占位符：{jobid} {interval_sec} {job_dir}"
        ),
    )
    collectors: List[Literal["sacct", "sinfo", "sstat", "scontrol"]] = Field(
        default_factory=lambda: ["sacct", "sinfo", "sstat", "scontrol"],
        description="循环采集的命令集合",
    )
    include_squeue_check: bool = Field(default=True, description="是否用 squeue 空队列作为结束条件之一")
    end_states: List[str] = Field(
        default_factory=lambda: ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"],
        description="从 sacct 读取到这些状态即认为作业结束",
    )
    run_seff_on_end: bool = Field(default=True, description="结束时是否生成一次 seff 日志（若命令存在）")


class MonitoringPlan(BaseModel):
    """
    PerfBench Agent 的核心计划：脚本注入 + 提交 + 登录节点监控。
    """

    scheduler: SchedulerType = Field(default="slurm", description="调度器类型（当前固定 slurm）")
    job_script: JobScriptPlan = Field(default_factory=JobScriptPlan)
    submit: SubmitPlan = Field(default_factory=SubmitPlan)
    monitor: MonitorPlan
    notes: Optional[str] = Field(default=None, description="给人类看的备注（不会影响执行）")


class ExecCommandResult(BaseModel):
    """
    外部命令执行结果（用于错误回传）。
    """

    cmd: List[str]
    cwd: Optional[str] = None
    returncode: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class ErrorBundle(BaseModel):
    """
    给 Fixer 的失败现场信息（尽量可复现）。
    """

    step: Literal["plan", "inject", "submit", "monitor", "report", "unknown"] = "unknown"
    message: str
    command: Optional[ExecCommandResult] = None
    available_commands: Dict[str, Optional[str]] = Field(
        default_factory=dict, description="命令可用性探测结果（cmd -> path/None）"
    )
    previous_plan: Optional[Dict[str, Any]] = None
    artifacts_excerpt: Dict[str, str] = Field(
        default_factory=dict,
        description="与错误相关的产物摘要（如监控脚本片段/关键日志片段），用于辅助修复",
    )


class FixSuggestion(BaseModel):
    """
    Fixer 给出的修改建议：用 dot-path 指向 plan 中需要替换的字段。

    例：
    changes = {
      "submit.jobid_regex": "Submitted batch job (\\d+)",
      "monitor.include_squeue_check": false
    }
    """

    summary: str = Field(..., description="一句话总结建议")
    changes: Dict[str, Any] = Field(default_factory=dict, description="dot-path -> 新值")
