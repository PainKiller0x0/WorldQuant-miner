# --- utils.py v18.6 (Absolute Defense DB Load) ---
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
        return _get_default_config()
    if os.path.isdir(SYSTEM_CONFIG_FILE):
        try: import shutil; shutil.rmtree(SYSTEM_CONFIG_FILE) 
        except: pass
        return _get_default_config() 
    config_data = _perform_safe_read(SYSTEM_CONFIG_FILE, _system_config_lock)
    if config_data is None: return _get_default_config()
    return config_data

def save_system_config(config_data):
    return _perform_safe_write(SYSTEM_CONFIG_FILE, _system_config_lock, config_data)

def _get_default_config():
    return {
        "miner_concurrency": 1, "evolver_concurrency": 1, "producer_queue_full_sleep": 10,
        "generation_interval_seconds": 45, "miner_use_local_model": True,
        "evolver_sample_size": 50, "evolver_wildcard_size": 10, "evolver_guidance_pool_size": 100,
        "pool_limit_unsubmitted": 50000, "pool_limit_submitted": 1000, "evolver_wildcard_count": 50, "hopeful_pool_max_size": 50000, 
        "llm_budgets": {"miner": {"daily_limit": 1500, "used_today": 0, "last_used_date_utc": "INIT"}, "evolver": {"daily_limit": 500, "used_today": 0, "last_used_date_utc": "INIT"}},
        "wq_api_limiter": {"current_tpm_limit": 60, "min_tpm_limit": 15, "max_tpm_limit": 200, "tpm_increment_on_success": 1, "tpm_decrement_factor_on_429": 0.75, "wq_429_cooldown_seconds": 60, "last_failure_timestamp": 0},
        "evolver_search_space": {}
    }

def load_hopeful_alphas_safe():
    try:
        config = load_system_config()
        pool_limit = max(1, int(config.get("hopeful_pool_max_size", 200)))
        with database.get_db() as db:
            # The database also contains the historical archive. Only load
            # the configured elite pool into the evolver process.
            alphas = (
                db.query(Alpha)
                .filter(Alpha.is_failed_on_wq == False)
                .order_by(Alpha.fitness.desc())
                .limit(pool_limit)
                .all()
            )
            result = []
            for a in alphas:
                # [v18.6 Fix] 绝对防御：先检查对象本身
                if not a: continue 
                
                try:
                    alpha_id = getattr(a, 'id', 'UNKNOWN')
                    
                    # 1. 处理 raw_data
                    if getattr(a, 'raw_data', None) is None:
                        data = {}
                    elif isinstance(a.raw_data, str):
                        try: data = json.loads(a.raw_data)
                        except: data = {}
                    else:
                        data = a.raw_data.copy()
                    
                    # 2. 补全字段 (使用 getattr 防止 AttributeError)
                    data['expression'] = getattr(a, 'expression', '')
                    data['checks_summary'] = getattr(a, 'checks_summary', '')
                    
                    if 'performance' not in data or not isinstance(data['performance'], dict):
                        data['performance'] = {}
                    
                    perf = data['performance']
                    perf['fitness'] = getattr(a, 'fitness', 0)
                    perf['sharpe'] = getattr(a, 'sharpe', 0)
                    perf['returns'] = getattr(a, 'returns', 0)
                    perf['turnover'] = getattr(a, 'turnover', 0)
                    
                    created_at = getattr(a, 'created_at', None)
                    data['timestamp'] = created_at.isoformat() if created_at else None
                    result.append(data)
                    
                except Exception as inner_e:
                    # 这里的 logger 不再引用 a 的属性，防止连环爆
                    logger.warning(f"[Utils-DB] 跳过损坏记录 (Parse Error): {inner_e}")
                    continue
            return result
    except Exception as e:
        logger.error(f"[Utils-DB] 加载 Alphas 失败: {e}"); return []

def save_hopeful_alphas_safe(alphas_list):
    if not isinstance(alphas_list, list): return False
    try:
        for alpha_data in alphas_list: database.add_alpha(alpha_data)
        config = load_system_config()
        limit_unsub = config.get("pool_limit_unsubmitted", 50000)
        limit_sub = config.get("pool_limit_submitted", 1000)
        database.trim_alphas(limit_unsub, limit_sub)
        return True
    except Exception as e:
        logger.error(f"[Utils-DB] 保存 Alphas 失败: {e}"); return False

def delete_alphas_safe(expressions):
    if not expressions: return 0
    try:
        with database.get_db() as db:
            batch_size = 100; total = 0
            for i in range(0, len(expressions), batch_size):
                batch = expressions[i:i+batch_size]
                total += db.query(Alpha).filter(Alpha.expression.in_(batch)).delete(synchronize_session=False)
            return total
    except Exception as e:
        logger.error(f"[Utils-DB] 删除 Alphas 失败: {e}"); return 0

def setup_logging(log_file):
    log_dir = "logs"
    if not os.path.exists(log_dir): os.makedirs(log_dir)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s', handlers=[
        logging.handlers.TimedRotatingFileHandler(os.path.join(log_dir, log_file), when='D', interval=1, backupCount=30, encoding='utf-8'),
        logging.StreamHandler()
    ])
