"""
PerfBench Agent 模块（实验性）。

该模块用于将“脚本注入/监控脚本生成/失败修复”这类策略性工作交给 LLM，
并通过 LangGraph 编排 Coder→Executor→Fixer 的闭环流程。

当前版本仅面向 Slurm（未来可扩展到 LSF/PBS 等）。
"""

