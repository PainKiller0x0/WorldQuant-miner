# --- utils.py v17.2 (Config: Evolver Tuning) ---
import logging
import logging.handlers 
import json
import os
import time
from filelock import FileLock
import random 
import database
from database import Alpha

SYSTEM_CONFIG_FILE = "system_config.json" 
SYSTEM_CONFIG_LOCK_FILE = "system_config.json.lock"
_system_config_lock = FileLock(SYSTEM_CONFIG_LOCK_FILE, timeout=10) 

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
                logger.warning(f"[Utils] 读取 {file_path} 失败 (尝试 {attempt+1}): {e}")
                time.sleep(random.uniform(0.1, 0.3))
        logger.error(f"[Utils] 严重: 读取 {file_path} 彻底失败!")
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
    if not os.path.exists(SYSTEM_CONFIG_FILE):
        logger.info("[Utils] 配置文件不存在，初始化默认值。")
        return _get_default_config()

    if os.path.isdir(SYSTEM_CONFIG_FILE):
        logger.warning(f"[Utils] 检测到 {SYSTEM_CONFIG_FILE} 是目录 (Docker挂载错误)，正在修正...")
        try:
            import shutil
            shutil.rmtree(SYSTEM_CONFIG_FILE) 
        except Exception as e:
            logger.error(f"[Utils] 无法删除错误目录: {e}")
            raise
        return _get_default_config() 

    config_data = _perform_safe_read(SYSTEM_CONFIG_FILE, _system_config_lock)
    if config_data is None:
        logger.warning(f"[Utils] 读取配置失败，尝试使用默认配置自愈。")
        return _get_default_config()
    return config_data

def save_system_config(config_data):
    return _perform_safe_write(SYSTEM_CONFIG_FILE, _system_config_lock, config_data)

def _get_default_config():
    return {
        "miner_concurrency": 1,
        "evolver_concurrency": 1,
        "producer_queue_full_sleep": 10,
        "generation_interval_seconds": 45,
        
        # [v17.4] Evolver 视野微调
        "evolver_sample_size": 50,       # 每次进化的父本候选池大小 (原20)
        "evolver_wildcard_size": 10,     # 其中包含的外卡(低分)数量 (原5)
        "evolver_guidance_pool_size": 100, # 策略导师分析的样本范围 (原20)

        "pool_limit_unsubmitted": 20000, # [v17.4] 默认调大，适应历史数据
        "pool_limit_submitted": 1000,
        "evolver_wildcard_count": 50,
        "hopeful_pool_max_size": 20000, 
        
        "llm_budgets": {
            "miner": {"daily_limit": 1500, "used_today": 0, "last_used_date_utc": "2024-01-01"},
            "evolver": {"daily_limit": 500, "used_today": 0, "last_used_date_utc": "2024-01-01"}
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

def load_hopeful_alphas_safe():
    try:
        with database.get_db() as db:
            alphas = db.query(Alpha).all()
            result = []
            for a in alphas:
                data = a.raw_data.copy() if a.raw_data else {}
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
        logger.error(f"[Utils-DB] 加载 Alphas 失败: {e}"); return []

def save_hopeful_alphas_safe(alphas_list):
    if not isinstance(alphas_list, list): return False
    success_count = 0
    try:
        for alpha_data in alphas_list:
            if database.add_alpha(alpha_data): success_count += 1
        if success_count > 0:
            logger.info(f"[Utils-DB] 新增入库 {success_count} 条策略。")
            config = load_system_config()
            limit_unsub = config.get("pool_limit_unsubmitted", config.get("hopeful_pool_max_size", 20000))
            limit_sub = config.get("pool_limit_submitted", 1000)
            deleted = database.trim_alphas(limit_unsub, limit_sub)
            if deleted > 0: logger.info(f"[Utils-DB] 双池整理完成，已清洗/修剪 {deleted} 条策略。")
        return True
    except Exception as e:
        logger.error(f"[Utils-DB] 保存 Alphas 失败: {e}"); return False

def delete_alphas_safe(expressions):
    if not expressions or not isinstance(expressions, list): return 0
    try:
        with database.get_db() as db:
            batch_size = 100; total_deleted = 0
            for i in range(0, len(expressions), batch_size):
                batch = expressions[i:i + batch_size]
                deleted = db.query(Alpha).filter(Alpha.expression.in_(batch)).delete(synchronize_session=False)
                total_deleted += deleted
            if total_deleted > 0: logger.info(f"[Utils-DB] 已从数据库物理删除 {total_deleted} 条策略。")
            return total_deleted
    except Exception as e:
        logger.error(f"[Utils-DB] 删除 Alphas 失败: {e}"); return 0

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
    logger.info(f"日志系统初始化完成 (v17.2 Economy-Edition)。")
