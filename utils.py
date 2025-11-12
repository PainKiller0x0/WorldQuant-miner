# --- utils.py v15.0 (Database Edition) ---
import logging
import logging.handlers 
import json
import os
import time
from filelock import FileLock
import random 

# 引入数据库模块
import database
from database import Alpha

# 基础配置 (保留 JSON 文件，方便手动改配置)
SYSTEM_CONFIG_FILE = "system_config.json" 
SYSTEM_CONFIG_LOCK_FILE = "system_config.json.lock"
_system_config_lock = FileLock(SYSTEM_CONFIG_LOCK_FILE, timeout=10) 

logger = logging.getLogger(__name__)

def _perform_safe_read(file_path, lock):
    """仅用于读取 system_config.json"""
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
                logger.warning(f"[Utils] 读取 {file_path} 失败 (尝试 {attempt+1}): {e}")
                time.sleep(random.uniform(0.1, 0.3))
        
        logger.error(f"[Utils] 严重: 读取 {file_path} 彻底失败!")
        return None

def _perform_safe_write(file_path, lock, data):
    """仅用于写入 system_config.json"""
    try:
        with lock:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
    except Exception as e:
        logger.error(f"[Utils] 写入失败 {file_path}: {e}")
        return False

def load_system_config():
    if not os.path.exists(SYSTEM_CONFIG_FILE):
        logger.info("[Utils] 配置文件不存在，初始化默认值。")
        return _get_default_config()

    config_data = _perform_safe_read(SYSTEM_CONFIG_FILE, _system_config_lock)
    
    if config_data is None:
        err_msg = f"[Utils] CRITICAL: 无法读取 {SYSTEM_CONFIG_FILE}。系统拒绝运行以防止数据重置。"
        logger.critical(err_msg)
        raise RuntimeError(err_msg) 
        
    return config_data

def save_system_config(config_data):
    return _perform_safe_write(SYSTEM_CONFIG_FILE, _system_config_lock, config_data)

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

# --- Database Adapters (核心修改) ---

def load_hopeful_alphas_safe():
    """
    从 SQLite 数据库加载所有 Alphas。
    为了兼容旧代码，这里返回一个 list of dicts。
    """
    try:
        with database.get_db() as db:
            # 查询所有 Alpha 对象
            alphas = db.query(Alpha).all()
            # 转换为旧代码习惯的 dict 格式，并还原 raw_data 中的额外字段
            result = []
            for a in alphas:
                data = a.raw_data.copy() if a.raw_data else {}
                # 确保关键字段从数据库列同步回来 (以防 raw_data 过期)
                data['expression'] = a.expression
                data['checks_summary'] = a.checks_summary
                if 'performance' not in data: data['performance'] = {}
                data['performance']['fitness'] = a.fitness
                data['performance']['sharpe'] = a.sharpe
                data['performance']['returns'] = a.returns
                data['performance']['turnover'] = a.turnover
                data['timestamp'] = a.created_at.isoformat() if a.created_at else None
                result.append(data)
            return result
    except Exception as e:
        logger.error(f"[Utils-DB] 加载 Alphas 失败: {e}")
        return []

def save_hopeful_alphas_safe(alphas_list):
    """
    将 Alpha 列表保存到数据库。
    Miner/Evolver 习惯传整个列表过来，我们这里做增量插入。
    """
    if not isinstance(alphas_list, list): return False
    
    success_count = 0
    try:
        for alpha_data in alphas_list:
            # 调用 database.py 的去重插入逻辑
            if database.add_alpha(alpha_data):
                success_count += 1
        
        if success_count > 0:
            logger.info(f"[Utils-DB] 新增入库 {success_count} 条策略。")
        return True
    except Exception as e:
        logger.error(f"[Utils-DB] 保存 Alphas 失败: {e}")
        return False

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
    logger.info(f"日志系统初始化完成 (v15.0 DB-Edition)。")