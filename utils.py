# --- utils.py v13.0 (Dual Watchdog) ---
# 共享工具模块: 日志, 配置读取

import logging
import json
import os
import threading # v13.0: 新增

# v8.0: 中心化配置
SYSTEM_CONFIG_FILE = "system_config.json" 

# --- v13.0: 新增线程安全锁 ---
# 用于保护对 system_config.json 的读写操作
_system_config_lock = threading.Lock()
# --- v13.0 结束 ---

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

def load_system_config():
    """
    (v13.0) 线程安全地读取并返回 system_config.json 的内容。
    """
    # v13.0: 增加锁
    with _system_config_lock:
        try:
            with open(SYSTEM_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            # 紧急回退 (Fallback)
            logger.error(f"读取 {SYSTEM_CONFIG_FILE} 失败: {e}。将使用紧急回退值！")
            # v13.0: 更新回退值以匹配新结构
            return {
                "miner_concurrency": 1,
                "evolver_concurrency": 1,
                "producer_queue_full_sleep": 10,
                "llm_budget": {
                    "comment": "看门狗 A: LLM API (Gemini) 每日预算",
                    "daily_budget_limit": 2000,
                    "budget_used_today": 0,
                    "budget_last_used_date_utc": "2024-01-01"
                },
                "wq_api_limiter": {
                    "comment": "看门狗 B: WQ API 动态令牌桶 (TPM)",
                    "initial_tpm_limit": 60,
                    "current_tpm_limit": 60,
                    "min_tpm_limit": 15,
                    "max_tpm_limit": 200,
                    "tpm_increment_on_success": 1,
                    "tpm_decrement_factor_on_429": 0.75,
                    "last_failure_timestamp": 0,
                    "seconds_to_wait_after_429": 60
                },
                "evolver_search_space": {}
            }

# --- v13.0: 新增配置保存函数 ---
def save_system_config(config_data):
    """
    (v13.0) 线程安全地将更新后的配置字典写回 system_config.json。
    """
    with _system_config_lock:
        try:
            with open(SYSTEM_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"保存 {SYSTEM_CONFIG_FILE} 失败: {e}", exc_info=True)
            return False
# --- v13.0 结束 ---

def setup_logging(log_file):
    """
    配置全局日志系统。(v13.0: 无变更)
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