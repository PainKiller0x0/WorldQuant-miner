# --- utils.py v14.2 (Debug & Anti-Reset) ---
import logging
import logging.handlers 
import json
import os
import time
from filelock import FileLock
import random 

# 基础配置
SYSTEM_CONFIG_FILE = "system_config.json" 
SYSTEM_CONFIG_LOCK_FILE = "system_config.json.lock"
_system_config_lock = FileLock(SYSTEM_CONFIG_LOCK_FILE, timeout=10) 

HOPEFUL_ALPHAS_FILE = "hopeful_alphas.json"
HOPEFUL_ALPHAS_LOCK_FILE = "hopeful_alphas.json.lock"
_hopeful_alphas_lock = FileLock(HOPEFUL_ALPHAS_LOCK_FILE, timeout=10)

logger = logging.getLogger(__name__)

def _perform_safe_read(file_path, lock):
    max_retries = 10
    with lock:
        for attempt in range(max_retries):
            try:
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content: return json.loads(content)
                elif attempt == 0 and not os.path.exists(file_path):
                    return None 
            except Exception as e:
                # v14.2: 增加 Debug 日志
                logger.warning(f"[Utils] 读取 {file_path} 失败 (尝试 {attempt+1}): {e}")
                time.sleep(random.uniform(0.1, 0.3))
        
        logger.error(f"[Utils] 严重: 读取 {file_path} 彻底失败 (已重试 {max_retries} 次)!")
        return None

def _perform_safe_write(file_path, lock, data):
    try:
        with lock:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
    except Exception as e:
        logger.error(f"[Utils] 写入失败 {file_path}: {e}")
        return False

def load_system_config():
    # 1. 首次初始化
    if not os.path.exists(SYSTEM_CONFIG_FILE):
        logger.info("[Utils] 配置文件不存在，初始化默认值。")
        return _get_default_config()

    # 2. 尝试读取
    config_data = _perform_safe_read(SYSTEM_CONFIG_FILE, _system_config_lock)
    
    # 3. (核心修复) 读取失败直接抛异常，禁止返回默认值覆盖！
    if config_data is None:
        err_msg = f"[Utils] CRITICAL: 无法读取 {SYSTEM_CONFIG_FILE}。系统拒绝运行以防止数据重置。"
        logger.critical(err_msg)
        raise RuntimeError(err_msg) # 宁可崩溃也不要重置数据
        
    return config_data

def _get_default_config():
    return {
        "miner_concurrency": 1,
        "evolver_concurrency": 1,
        "producer_queue_full_sleep": 10,
        "hopeful_pool_max_size": 200,
        "llm_budgets": {
            "miner": {"daily_limit": 3000, "used_today": 0, "last_used_date_utc": "2024-01-01"},
            "evolver": {"daily_limit": 1000, "used_today": 0, "last_used_date_utc": "2024-01-01"}
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
    # v14.2: 增加 Debug 日志，监控谁在写入
    # logger.info(f"[Utils] 正在写入 system_config.json...") 
    return _perform_safe_write(SYSTEM_CONFIG_FILE, _system_config_lock, config_data)

def load_hopeful_alphas_safe():
    data = _perform_safe_read(HOPEFUL_ALPHAS_FILE, _hopeful_alphas_lock)
    if data is None: return []
    return data.get("alphas", []) if isinstance(data, dict) else data

def save_hopeful_alphas_safe(alphas_list):
    return _perform_safe_write(HOPEFUL_ALPHAS_FILE, _hopeful_alphas_lock, {"alphas": alphas_list})

def setup_logging(log_file):
    log_dir = "logs"
    if not os.path.exists(log_dir): os.makedirs(log_dir)
    for handler in logging.root.handlers[:]: logging.root.removeHandler(handler)
    log_formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
    main_log_path = os.path.join(log_dir, log_file)
    main_handler = logging.handlers.TimedRotatingFileHandler(main_log_path, when='D', interval=1, backupCount=30, encoding='utf-8')
    main_handler.setLevel(logging.INFO); main_handler.setFormatter(log_formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO); console_handler.setFormatter(log_formatter)
    logging.basicConfig(level=logging.INFO, handlers=[main_handler, console_handler])
    base_name = os.path.splitext(log_file)[0]
    issue_log_file = f"{base_name}_issues.log"
    issue_log_path = os.path.join(log_dir, issue_log_file)
    issue_handler = logging.handlers.TimedRotatingFileHandler(issue_log_path, when='D', interval=1, backupCount=30, encoding='utf-8')
    issue_handler.setLevel(logging.WARNING); issue_handler.setFormatter(log_formatter)
    logging.getLogger('').addHandler(issue_handler)
    logger.info(f"日志系统初始化完成 (v13.2.1: 每日轮转, 保留 {main_handler.backupCount} 天)。")
    logger.info(f"WARNING及以上的问题将额外记录到: {issue_log_path} (同样轮转)")