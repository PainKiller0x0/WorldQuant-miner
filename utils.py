# --- utils.py ---
# 共享工具模块: 日志, 配置读取

import logging
import json
import os

# v8.0: 中心化配置
SYSTEM_CONFIG_FILE = "system_config.json" 

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

def load_system_config():
    """
    (v8.0) 读取并返回 system_config.json 的内容。
    注意: 这会在每次需要时都读取文件，以获取动态参数。
    """
    try:
        with open(SYSTEM_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        # 紧急回退 (Fallback)
        logger.error(f"读取 {SYSTEM_CONFIG_FILE} 失败: {e}。将使用紧急回退值！")
        return {
            "wq_api_cooldown": 120,
            "llm_api_cooldown": 5400,
            "miner_concurrency": 1,
            "miner_sleep": 120,
            "evolver_concurrency": 1,
            "evolver_sleep": 120
        }

def setup_logging(log_file):
    """
    配置全局日志系统。
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 清理旧的处理器，避免日志重复
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # --- 基础配置 (INFO及以上，输出到文件和控制台) ---
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s', # v7.7: 添加 threadName
                        handlers=[
                            logging.FileHandler(os.path.join(log_dir, log_file)),
                            logging.StreamHandler()
                        ])

    # --- 问题日志处理器 (WARNING及以上) ---
    base_name = os.path.splitext(log_file)[0]
    issue_log_file = f"{base_name}_issues.log"
    issue_log_path = os.path.join(log_dir, issue_log_file)

    issue_handler = logging.FileHandler(issue_log_path)
    issue_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s') # v7.7: 添加 threadName
    issue_handler.setFormatter(formatter)

    logging.getLogger('').addHandler(issue_handler)
    
    # 使用 __name__ (即 'utils') 的 logger 记录
    logger.info("日志系统初始化完成。INFO及以上信息将输出到控制台和主日志文件。")
    logger.info(f"WARNING及以上的问题将额外记录到: {issue_log_path}")