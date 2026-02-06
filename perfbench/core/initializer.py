#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import platform
import shutil
import time
from perfbench.utils.logger import get_logger
from perfbench.utils.env_loader import load_perfbench_env
from perfbench.utils.system_checker import check_slurm_environment, get_architecture

logger = get_logger()

def initialize_environment(force: bool = False):
    """
    初始化PerfBench运行环境
    - 检查系统架构
    - 安装对应架构的依赖
    - 配置必要的环境变量
    """
    logger.info("开始初始化PerfBench环境...")

    # 初始化阶段加载 .env（用于 API 配置等环境变量）
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    loaded_env_map = load_perfbench_env(project_root=project_root, override=False)
    if loaded_env_map:
        for env_path, keys in loaded_env_map.items():
            logger.info(f"已加载环境变量文件: {env_path}，键数量: {len(keys)}")
    else:
        logger.info("未发现可加载的 .env 文件（已跳过）")
    
    # 检查系统架构
    arch = get_architecture()
    logger.info(f"检测到系统架构: {arch}")
    
    # 检查SLURM环境
    if not check_slurm_environment():
        # 如果第一次检测失败且未启用强制模式，则建议使用 --force 或检查命令路径
        logger.error("未检测到SLURM环境或 SLURM 命令不可用，请确保工具运行在SLURM集群登录节点上，或使用 --force 以跳过此检查（仅用于调试）")
        if not force:
            return False
        logger.warning("继续初始化（force=True）")
        
    # 创建工具配置目录
    config_dir = os.path.expanduser('~/.perfbench')
    os.makedirs(config_dir, exist_ok=True)
    
    # 复制对应架构的依赖库
    lib_path = os.path.join(os.path.dirname(__file__), '..', 'libs', arch)
    if os.path.exists(lib_path):
        for lib in os.listdir(lib_path):
            src = os.path.join(lib_path, lib)
            dst = os.path.join(config_dir, 'libs', lib)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    
    logger.info("环境初始化完成")
    return True
