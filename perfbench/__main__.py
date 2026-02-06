#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Package entry point for perfbench.

This module contains the CLI parsing and top-level orchestration.
"""
from datetime import datetime
import os
import sys
import argparse
import math
from perfbench.core.initializer import initialize_environment
from perfbench.core.script_processor import process_slurm_script, process_sunway_script
from perfbench.core.validator import validate_environment
from perfbench.utils.env_loader import load_perfbench_env
from perfbench.utils.logger import setup_logging
from perfbench.utils.progress_bar import StepProgress


def parse_arguments():
    parser = argparse.ArgumentParser(description='PerfBench - 超算集群性能基准测试工具')
    parser.add_argument('-init', action='store_true', help='初始化工具环境')
    parser.add_argument('-s', '--script', type=str, help='作业提交脚本路径')
    parser.add_argument('-t', '--interval', type=int, help='性能采集时间间隔（秒）')
    parser.add_argument('-o', '--output', type=str, help='输出目录路径')
    parser.add_argument('-v', action='store_true', help='运行工具适配性测试')
    parser.add_argument('--force', action='store_true', help='跳过环境检测（仅用于调试）')
    parser.add_argument('-sw', action='store_true', help='指定为申威平台（默认自动检测）')

    # Agent（实验性）：使用 LLM 生成脚本注入与监控策略（当前仅支持 Slurm）
    parser.add_argument('--agent', action='store_true', help='启用 agent 模式（DeepSeek + LangChain/LangGraph）')
    parser.add_argument('--agent-model', type=str, default=None, help='DeepSeek 模型名（默认读 DEEPSEEK_MODEL）')
    parser.add_argument('--agent-base-url', type=str, default=None, help='DeepSeek Base URL（默认读 DEEPSEEK_BASE_URL）')
    parser.add_argument('--agent-temperature', type=float, default=None, help='LLM 温度（默认 0）')
    parser.add_argument('--agent-max-fix-rounds', type=int, default=2, help='失败后最大修复轮次（默认 2）')
    parser.add_argument('--version', action='version', version='%(prog)s 1.0.0')
    return parser


def main():
    parser = parse_arguments()
    args = parser.parse_args()
    logger = setup_logging()

    # 启动时加载 .env，确保 agent/API 相关变量在每次运行都可用
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loaded_env_map = load_perfbench_env(project_root=project_root, override=False)
    if loaded_env_map:
        for env_path, keys in loaded_env_map.items():
            logger.info(f"已加载环境变量文件: {env_path}，键数量: {len(keys)}")

    # CLI主流程进度条步骤
    steps = [
        "读取用户提交脚本",
        "监控脚本生成中",
        "作业提交",
        "监控中",
        "监控完成",
        "报告生成中",
        "报告生成完成"
    ]
    progress = StepProgress(steps)

    try:
        if args.init:
            initialize_environment(force=args.force)
            return

        if args.v:
            validate_environment(force=args.force)
            return

        if args.script:
            if not args.interval or not args.output:
                logger.error("请提供采集间隔(-t)和输出目录(-o)参数")
                sys.exit(1)
            progress.next()  # 1. 读取用户提交脚本
            # 解析和生成监控脚本
            progress.next("监控脚本生成中")  # 2. 监控脚本生成中

            # agent 模式：当前只支持 Slurm（非申威）
            if args.agent:
                if args.sw:
                    logger.error("agent 模式当前仅支持 Slurm（未支持申威平台）")
                    sys.exit(1)
                try:
                    from perfbench.agent.llm import load_deepseek_config_from_env
                    from perfbench.agent.workflow import run_slurm_agent_workflow

                    deepseek_cfg = load_deepseek_config_from_env(
                        api_key=None,
                        base_url=args.agent_base_url,
                        model=args.agent_model,
                        temperature=args.agent_temperature,
                    )
                    job_dir, script_info = run_slurm_agent_workflow(
                        script_path=args.script,
                        interval_sec=args.interval,
                        output_path=args.output,
                        deepseek_cfg=deepseek_cfg,
                        max_fix_rounds=args.agent_max_fix_rounds,
                    )
                except Exception as e:
                    logger.error(f"agent 模式执行失败: {str(e)}")
                    sys.exit(1)

                progress.next("作业提交")  # 3. 作业提交
                progress.next("监控中")  # 4. 监控中
                progress.next("监控完成")  # 5. 监控完成
                logger.info(f"PerfBench(agent)流程已完成，输出目录: {job_dir}")
                progress.next("报告生成中")  # 6. 报告生成中
                generate_certificate_for_test(logger, job_dir, script_info, args)
                progress.finish()  # 7. 报告生成完成
                return

            if not args.sw:
                # process_slurm_script 内部包含所有后续步骤（除了报告生成）
                job_dir, script_info = process_slurm_script(args.script, args.interval, args.output)
                """
                info = {
                    'job_name': None,
                    'nodes': 1,
                    'tasks_per_node': 1,
                    'cpus_per_task': 1,
                    'time_limit': None,
                    'partition': None,
                    'output': None,
                    'error': None,
                    'commands': []
                }
                """
                progress.next("作业提交")  # 3. 作业提交
                # 监控中（此处为启动监控脚本后）
                progress.next("监控中")  # 4. 监控中
                # 监控完成（此处可根据后处理或监控脚本退出信号完善）
                progress.next("监控完成")  # 5. 监控完成
                logger.info(f"PerfBench流程已完成，输出目录: {job_dir}")
                progress.next("报告生成中")  # 6. 报告生成中
                generate_certificate_for_test(logger, job_dir, script_info, args)
                progress.finish()  # 7. 报告生成完成
                return
            else:
                job_dir, script_info = process_sunway_script(args.script, args.interval, args.output)
                """
                info = {
                    'job_name': None,
                    'nodes': 1,
                    'tasks_per_node': 1,
                    'cpus_per_task': 1,
                    'time_limit': None,
                    'partition': None,
                    'output': None,
                    'error': None,
                    'commands': []
                }
                """
                progress.next("作业提交")  # 3. 作业提交
                # 监控中（此处为启动监控脚本后）
                progress.next("监控中")  # 4. 监控中
                # 监控完成（此处可根据后处理或监控脚本退出信号完善）
                progress.next("监控完成")  # 5. 监控完成
                logger.info(f"PerfBench流程已完成，输出目录: {job_dir}")
                progress.finish()  # 6. 报告生成完成
                # progress.next("报告生成中")  # 6. 报告生成中
                # generate_certificate_for_test(logger, job_dir, script_info, args)
                # progress.finish()  # 7. 报告生成完成
                return
        # 如果没有提供任何参数，显示帮助信息
        parser.print_help()

    except Exception as e:
        logger.error(f"执行过程中发生错误: {str(e)}")
        sys.exit(1)


def generate_certificate_for_test(logger, job_dir, script_info, args):
    # 延迟导入：避免在仅查看 --help/--version 时因缺少依赖而崩溃
    try:
        from perfbench.utils.result_handler import calculate_parallelism, get_platform_config, Result
    except ModuleNotFoundError as e:
        logger.warning(f"缺少依赖导致无法生成报告（请安装相关依赖后重试）：{str(e)}")
        return

    try:
        from perfbench.report.certificate_generator import generate_certificate
    except ModuleNotFoundError as e:
        logger.warning(f"缺少依赖导致无法生成证书 PDF（请安装 pypdf/reportlab 后重试）：{str(e)}")
        return

    platform_config = get_platform_config()  # 获取平台配置-platform_config.yaml
    if not platform_config:
        logger.warning("平台配置读取失败，跳过报告生成")
        return

    parallelism_info = calculate_parallelism(platform_name=platform_config['platform_name'], node_num=script_info['nodes'])
    logger.info(f"计算得到的并行度: {parallelism_info}")
            
    # 解析 sacct 结果：监控脚本可能是后台启动的，日志生成存在竞争，需做容错
    try:
        sacct_result = Result(cmd_name="sacct", out_dir=job_dir, interval=args.interval)
        sacct_result.parse_sacct()
        elapsed_time = sacct_result.get_elapsed_time() # 本次作业的运行时间
    except Exception as e:
        logger.warning(f"当前尚无法解析 sacct 日志（可能作业仍在运行或日志未生成）：{str(e)}")
        return
            
    para_eff = float(
    float(platform_config["compared_cores"] * platform_config["compared_run_time"])
        / float((parallelism_info["core_num"] // 10000) * elapsed_time)
    ) * 100
            
    report_info = {
        "platform": platform_config["platform_name"],
        "node_num": script_info['nodes'],
        "app_name": script_info['job_name'],
        "core_num": parallelism_info["core_num"],
        "eff": f"{para_eff:.2f}%({platform_config['compared_cores']} Nodes)",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    generate_certificate(report_info, job_dir)

if __name__ == '__main__':
    main()
