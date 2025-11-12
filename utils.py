# --- utils.py v13.3.6 (修复 hopeful_alphas 加载格式 Bug) ---
import logging
import logging.handlers 
import json
import os
import threading 
import time
from filelock import FileLock # v13.3.1
import random 

# v8.0: 中心化配置
SYSTEM_CONFIG_FILE = "system_config.json" 
# v13.3.1: 跨进程锁
SYSTEM_CONFIG_LOCK_FILE = "system_config.json.lock"
_system_config_lock = FileLock(SYSTEM_CONFIG_LOCK_FILE, timeout=10) 

# --- v13.3.5: 为 Hopeful Alphas 添加保险 ---
HOPEFUL_ALPHAS_FILE = "hopeful_alphas.json"
HOPEFUL_ALPHAS_LOCK_FILE = "hopeful_alphas.json.lock"
_hopeful_alphas_lock = FileLock(HOPEFUL_ALPHAS_LOCK_FILE, timeout=10)
# --- v13.3.5 结束 ---

logger = logging.getLogger(__name__)

# --- v13.3.5: 启动重试逻辑 (从 load_system_config 中提取) ---
def _perform_safe_read(file_path, lock):
    """
    (v13.3.5) 通用的安全读取函数
    包含 v13.2.3 (启动重试) 和 v13.3.1 (文件锁)
    """
    max_retries = 3       
    retry_delay_seconds = 2 

    with lock:
        for attempt in range(max_retries):
            try:
                # 检查文件是否存在且非空
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        # 再次检查内容，防止 race condition
                        if content: 
                            return json.loads(content)
                        else:
                             logger.warning(f"读取 {file_path} 成功，但内容为空 (第 {attempt + 1}/{max_retries} 次)。")
                else:
                    logger.warning(f"读取 {file_path} 失败 (第 {attempt + 1}/{max_retries} 次): 文件未找到或为空。Docker 卷可能正在挂载...")

            except json.JSONDecodeError:
                 logger.warning(f"读取 {file_path} 失败 (第 {attempt + 1}/{max_retries} 次): 文件内容为空或 JSON 格式损坏。")
            except Exception as e:
                logger.error(f"读取 {file_path} 时发生意外错误 (第 {attempt + 1}/{max_retries} 次): {e}")

            # 如果不是最后一次尝试，则等待
            if attempt < max_retries - 1:
                logger.info(f"将在 {retry_delay_seconds} 秒后重试 {file_path}...")
                time.sleep(retry_delay_seconds)
        
        # 所有重试均失败
        logger.error(f"所有 {max_retries} 次读取 {file_path} 的尝试均失败。")
        return None # 返回 None，由调用者处理回退值

def _perform_safe_write(file_path, lock, data):
    """
    (v13.3.5) 通用的安全写入函数 (使用文件锁)
    """
    try:
        with lock:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
    except Exception as e:
        logger.error(f"安全写入 {file_path} 失败: {e}", exc_info=True)
        return False
# --- v13.3.5 结束 ---


def load_system_config():
    """
    (v13.3.5) 线程安全 + 跨进程安全地读取 system_config.json。
    """
    config_data = _perform_safe_read(SYSTEM_CONFIG_FILE, _system_config_lock)
    
    if config_data is not None:
        return config_data
        
    # --- 紧急回退 (Fallback) ---
    logger.error(f"读取 {SYSTEM_CONFIG_FILE} 失败，将使用紧急回退值！")
    return {
        "miner_concurrency": 1,
        "evolver_concurrency": 1,
        "producer_queue_full_sleep": 10,
        "hopeful_pool_max_size": 200,
        # --- v14.0: 双轨制预算默认值 ---
        "llm_budgets": {
            "miner": {
                "daily_limit": 3000,
                "used_today": 0,
                "last_used_date_utc": "2024-01-01"
            },
            "evolver": {
                "daily_limit": 1000,
                "used_today": 0,
                "last_used_date_utc": "2024-01-01"
            }
        },
        # 兼容旧版 (可选保留，防止报错)
        "llm_budget": {
            "daily_budget_limit": 2000,
            "budget_used_today": 0,
            "budget_last_used_date_utc": "2024-01-01"
        },
        # ---------------------------
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
    (v13.3.5) 线程安全 + 跨进程安全地将配置写回 system_config.json。
    """
    return _perform_safe_write(SYSTEM_CONFIG_FILE, _system_config_lock, config_data)


# --- v13.3.6: 修复 Hopeful Alphas 的加载函数 ---
def load_hopeful_alphas_safe():
    """
    (v13.3.6) 线程安全 + 跨进程安全地读取 hopeful_alphas.json。
    包含启动重试逻辑。
    修复了 v13.3.5 的 Bug，现在兼容旧的 [...] 格式和新的 {"alphas": [...]} 格式。
    """
    alphas_data = _perform_safe_read(HOPEFUL_ALPHAS_FILE, _hopeful_alphas_lock)
    
    if alphas_data is None:
        logger.error(f"读取 {HOPEFUL_ALPHAS_FILE} 失败，将使用空列表回退。")
        return []
    
    # --- v13.3.6: 关键修复 ---
    # 检查加载的数据是字典还是列表
    if isinstance(alphas_data, dict):
        # 它是 {"alphas": [...]} 结构 (新格式)
        return alphas_data.get("alphas", [])
    elif isinstance(alphas_data, list):
        # 它是 [...] 结构 (你的 7 天前备份)
        logger.warning(f"检测到旧的 'list' 格式 {HOPEFUL_ALPHAS_FILE}。下次保存时将自动迁移到 'dict' 格式。")
        return alphas_data
    else:
        # 结构未知
        logger.error(f"读取 {HOPEFUL_ALPHAS_FILE} 失败：文件不是字典或列表。将使用空列表。")
        return []
    # --- v13.3.6: 结束 ---

def save_hopeful_alphas_safe(alphas_list):
    """
    (v13.3.5) 线程安全 + 跨进程安全地将 Alpha 列表写回 hopeful_alphas.json。
    (v13.3.6) 此函数始终写入新格式 {"alphas": [...]}，这是正确的。
    """
    # 始终保存为新格式
    data_to_save = {"alphas": alphas_list}
    return _perform_safe_write(HOPEFUL_ALPHAS_FILE, _hopeful_alphas_lock, data_to_save)
# --- v13.3.6: 结束 ---


def setup_logging(log_file):
    """
    v13.2.1: 实施日志轮转 (Log Rotation)
    (此函数保持不变)
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    log_formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
    main_log_path = os.path.join(log_dir, log_file)
    main_handler = logging.handlers.TimedRotatingFileHandler(
        main_log_path, when='D', interval=1, backupCount=30, encoding='utf-8'
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(log_formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_formatter)
    logging.basicConfig(level=logging.INFO, handlers=[main_handler, console_handler])
    base_name = os.path.splitext(log_file)[0]
    issue_log_file = f"{base_name}_issues.log"
    issue_log_path = os.path.join(log_dir, issue_log_file)
    issue_handler = logging.handlers.TimedRotatingFileHandler(
        issue_log_path, when='D', interval=1, backupCount=30, encoding='utf-8'
    )
    issue_handler.setLevel(logging.WARNING)
    issue_handler.setFormatter(log_formatter)
    logging.getLogger('').addHandler(issue_handler)
    logger.info(f"日志系统初始化完成 (v13.2.1: 每日轮转, 保留 {main_handler.backupCount} 天)。")
    logger.info(f"WARNING及以上的问题将额外记录到: {issue_log_path} (同样轮转)")