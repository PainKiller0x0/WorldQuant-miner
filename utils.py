# --- utils.py v13.2.3 (竞争条件修复) ---
# 共享工具模块: 日志, 配置读取

import logging
import logging.handlers 
import json
import os
import threading 
import time  # <-- v13.2.3: 引入 time 模块用于重试

# v8.0: 中心化配置
SYSTEM_CONFIG_FILE = "system_config.json" 

# --- v13.0: 线程安全锁 ---
_system_config_lock = threading.Lock()
# --- v13.0 结束 ---

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

def load_system_config():
    """
    (v13.2.3) 线程安全地读取 system_config.json，
    增加了重试逻辑以防止 Docker 卷挂载竞争条件。
    """
    # --- v13.2.3: 竞争条件修复配置 ---
    max_retries = 3       # 重试 3 次
    retry_delay_seconds = 2 # 每次间隔 2 秒
    # --- v13.2.3 结束 ---

    with _system_config_lock:
        # --- v13.2.3: 增加重试循环 ---
        for attempt in range(max_retries):
            try:
                # 检查文件是否存在且非空
                if os.path.exists(SYSTEM_CONFIG_FILE) and os.path.getsize(SYSTEM_CONFIG_FILE) > 0:
                    with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                        # 成功读取，立即返回
                        logger.info(f"成功读取 {SYSTEM_CONFIG_FILE} (尝试 {attempt + 1}/{max_retries})")
                        return config_data
                else:
                    logger.warning(f"读取 {SYSTEM_CONFIG_FILE} 失败 (第 {attempt + 1}/{max_retries} 次): 文件未找到或为空。Docker 卷可能正在挂载...")

            except json.JSONDecodeError:
                 logger.warning(f"读取 {SYSTEM_CONFIG_FILE} 失败 (第 {attempt + 1}/{max_retries} 次): 文件内容为空或 JSON 格式损坏。")
            except Exception as e:
                # 捕获其他潜在异常
                logger.error(f"读取 {SYSTEM_CONFIG_FILE} 时发生意外错误 (第 {attempt + 1}/{max_retries} 次): {e}")

            # 如果不是最后一次尝试，则等待
            if attempt < max_retries - 1:
                logger.info(f"将在 {retry_delay_seconds} 秒后重试...")
                time.sleep(retry_delay_seconds)
        # --- v13.2.3 循环结束 ---

        # --- 紧急回退 (Fallback) ---
        # 如果所有重试均失败
        logger.error(f"所有 {max_retries} 次读取 {SYSTEM_CONFIG_FILE} 的尝试均失败。将使用紧急回退值！")
        # v13.1: 更新回退值 (保持不变)
        return {
            "miner_concurrency": 1,
            "evolver_concurrency": 1,
            "producer_queue_full_sleep": 10,
            "hopeful_pool_max_size": 200, # 这就是为什么你看到了 200
            "llm_budget": {
                "daily_budget_limit": 2000,
                "budget_used_today": 0, # 这就是为什么预算清零了
                "budget_last_used_date_utc": "2024-01-01"
            },
            "wq_api_limiter": {
                "current_tpm_limit": 60,
                "min_tpm_limit": 15,
                "max_tpm_limit": 200,
                "tpm_increment_on_success": 1,
                "tpm_decrement_factor_on_429": 0.75,
                "wq_429_cooldown_seconds": 60,
                "last_failure_timestamp": 0
            },
            "evolver_search_space": {}
        }

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
    v13.2.1: 实施日志轮转 (Log Rotation)
    (此函数保持不变)
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 清理旧的处理器，避免日志重复
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    log_formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')

    # --- v13.2.1: 主日志处理器 (TimedRotatingFileHandler) ---
    main_log_path = os.path.join(log_dir, log_file)
    main_handler = logging.handlers.TimedRotatingFileHandler(
        main_log_path, 
        when='D',           # 按天轮转 (Daily)
        interval=1,         # 每天
        backupCount=30,     # 保留 30 天的日志
        encoding='utf-8'
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(log_formatter)
    
    # 控制台处理器 (保持不变)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_formatter)

    # --- 基础配置 (INFO及以上) ---
    logging.basicConfig(level=logging.INFO,
                        handlers=[
                            main_handler,     # v13.2.1: 使用新的轮转处理器
                            console_handler
                        ])

    # --- v13.2.1: 问题日志处理器 (TimedRotatingFileHandler) ---
    base_name = os.path.splitext(log_file)[0]
    issue_log_file = f"{base_name}_issues.log"
    issue_log_path = os.path.join(log_dir, issue_log_file)

    issue_handler = logging.handlers.TimedRotatingFileHandler(
        issue_log_path,
        when='D',           # 按天N轮转
        interval=1,         # 每天
        backupCount=30,     # 同样保留 30 天
        encoding='utf-8'
    )
    issue_handler.setLevel(logging.WARNING) # 只记录 WARNING 及以上
    issue_handler.setFormatter(log_formatter)

    # 将 issue_handler 添加到根 logger
    logging.getLogger('').addHandler(issue_handler)
    
    logger.info(f"日志系统初始化完成 (v13.2.1: 每日轮转, 保留 {main_handler.backupCount} 天)。")
    logger.info(f"WARNING及以上的问题将额外记录到: {issue_log_path} (同样轮转)")