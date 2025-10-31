# --- Web仪表盘.py v12.1.4 (修复图表“0 提交” Bug) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
# v12.1.0: 引入 Counter
from collections import deque, Counter
import os.path
import logging
import pandas as pd
import numpy as np

# --- v12.1.4: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v12.1.4"
# --- v12.1.4: 结束 ---

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
# v11.0.5: 文件路径调整
FAILED_SUBMISSIONS_FILE_OLD = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json')) # 旧文件路径
SUBMISSION_FAILURE_LOG_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submission_failure_log.json')) # 新日志文件路径
# v11.0.5: 结束
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
DASHBOARD_FILE_PATH = os.path.join(BASE_DIR, "Web仪表盘.py")
SYSTEM_CONFIG_FILE = os.path.join(BASE_DIR, 'system_config.json')
TESTED_ALPHAS_LOG_FILE = os.path.join(BASE_DIR, 'tested_alphas_log.json')

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
CACHE_DURATION = timedelta(seconds=300)

file_lock = threading.Lock()
hopeful_lock = threading.Lock()
failure_log_lock = threading.Lock() # v11.0.5: 用于新日志
config_lock = threading.Lock()
tested_log_lock = threading.Lock()

_timeseries_cache = None
_timeseries_cache_time = None
_cache_lock = threading.Lock()

# v12.1.0: 新增每日统计的缓存
_submission_cache = None
_submission_cache_time = None
_submission_cache_lock = threading.Lock()
# v12.1.0: 结束

# --- v12.1.4: 升级 Load/Save (修复僵尸数据) ---
def load_submitted_alphas():
    """ (v12.1.4) 修复 v12.1.2 中错误的迁移逻辑 """
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): return {} # 返回空 dict
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return {} # 返回空 dict
            
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            final_dict = {}
            needs_resave = False # 标记是否需要回写
            migration_timestamp = None # v12.1.4: 用于标记 v11 迁移的时间戳
            
            # Path 1: 数据是 list (v11.x 旧格式)，必须迁移
            if isinstance(data, (list, set)):
                logger.warning(f"[Submit Load] Old data format (list) detected in {filepath}. Migrating...")
                migration_timestamp = datetime.now(timezone.utc).isoformat() # v12.1.4: 记录此次迁移的时间
                for expr in data:
                    if isinstance(expr, str):
                        # v12.1.2: 迁移时添加 "reason" 标记
                        final_dict[expr] = {
                            "manual_timestamp": migration_timestamp,
                            "reason": "MIGRATED_UNKNOWN" 
                        }
                needs_resave = True
            
            # Path 2: 数据已经是 dict (v12.0.0+ 新格式)
            elif isinstance(data, dict):
                final_dict = data
                
                # v12.1.4: 寻找 v12.0.0 迁移时（当时没有 reason 键）的那个时间戳
                # 找到所有没有 "reason" 标记的时间戳
                timestamps_no_reason = [
                    item.get('manual_timestamp') 
                    for item in final_dict.values() 
                    if isinstance(item, dict) and "reason" not in item
                ]
                
                if timestamps_no_reason:
                    # 找到出现次数最多的时间戳，那一定是 v12.0.0 的迁移时间戳
                    zombie_timestamp = Counter(timestamps_no_reason).most_common(1)[0][0]
                    logger.warning(f"[Submit Load] Found v12.0.0 migrated data (Timestamp: {zombie_timestamp}). Upgrading to v12.1.4 flags...")
                    needs_resave = True
                    
                    for item in final_dict.values():
                        if isinstance(item, dict) and "reason" not in item:
                            if item.get('manual_timestamp') == zombie_timestamp:
                                item["reason"] = "MIGRATED_UNKNOWN" # 标记为僵尸
                            else:
                                # 这是 v12.0.0-v12.1.1 期间的手动提交，标记为真人
                                item["reason"] = "MANUAL_ADD" 
                        
            else:
                logger.warning(f"[Submit Load] File {filepath} bad format (not dict or list)."); return {}

            # 如果执行了任何迁移，立即回写
            if needs_resave:
                logger.info(f"[Submit Load] Resaving {filepath} with new 'reason' flags...")
                try:
                    with open(filepath, 'w', encoding='utf-8') as f_save:
                        json.dump(final_dict, f_save, indent=4)
                    logger.info(f"Successfully migrated/updated {len(final_dict)} entries with 'reason' flags.")
                except Exception as save_e:
                    logger.error(f"Failed to save migrated data for {filepath}! {save_e}")
            
            return final_dict
            
        except Exception as e:
            logger.error(f"[Submit Load] Error loading {filepath}: {e}", exc_info=False); return {}

def save_submitted_alphas(submitted_dict):
    """ (v12.0.0) 保存 submitted_alphas.json (现在保存 dict) """
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        try:
            if not isinstance(submitted_dict, dict): # 检查 dict
                 logger.error(f"[Submit Save] Invalid data type: {type(submitted_dict)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(submitted_dict, f, indent=4) # 保存 dict
            return True
        except Exception as e: logger.error(f"[Submit Save] Error saving {filepath}: {e}", exc_info=False); return False
# --- v12.1.4: 结束 ---

# --- v11.0.11: 新增 hopeful_alphas 的读写函数 ---
def load_hopeful_alphas_list():
    """ (v11.0.11) 辅助函数: 安全地读取 hopeful_alphas.json """
    with hopeful_lock:
        filepath = HOPEFUL_ALPHAS_FILE
        if not (os.path.exists(filepath) and os.path.isfile(filepath) and os.path.getsize(filepath) > 2):
            return []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            else:
                logger.warning(f"[Hopeful Load] {filepath} is not a list. Returning empty.")
                return []
        except Exception as e:
            logger.error(f"[Hopeful Load] Error loading {filepath}: {e}", exc_info=False)
            return []

def save_hopeful_alphas_list(alphas_list):
    """ (v11.0.11) 辅助函数: 安全地写入 hopeful_alphas.json """
    with hopeful_lock:
        filepath = HOPEFUL_ALPHAS_FILE
        try:
            if not isinstance(alphas_list, list):
                 logger.error(f"[Hopeful Save] Invalid data type: {type(alphas_list)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(alphas_list, f, indent=4)
            return True
        except Exception as e:
            logger.error(f"[Hopeful Save] Error saving {filepath}: {e}", exc_info=False); return False

# --- v11.0.10: 修复的失败日志加载器 ---
def load_failed_submissions_old_format():
    """ (v11.0.10) 辅助函数，只在迁移时由 load_submission_failures 调用 """
    filepath = FAILED_SUBMISSIONS_FILE_OLD
    if not (os.path.exists(filepath) and os.path.isfile(filepath)): return set() # 强化检查
    try:
        # 简化检查
        if os.path.getsize(filepath) < 2: return set()
        with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
        if isinstance(data, (list, set)):
            return set(data) # 确保返回 set
        else:
             logger.warning(f"[Old Failed Load] File {filepath} not list/set.")
             return set()
    except Exception as e:
        logger.error(f"[Old Failed Load] Error loading {filepath}: {e}")
    return set()

def load_submission_failures():
    """ (v12.1.4) 修复“僵尸”Alpha (迁移) Bug, 并为 v12.1.2 添加 "reason" 标记 """
    with failure_log_lock:
        new_filepath = SUBMISSION_FAILURE_LOG_FILE
        old_filepath = FAILED_SUBMISSIONS_FILE_OLD
        
        current_failures_list = []
        needs_resave = False # v12.1.2: 标记是否需要回写
        
        new_file_exists = os.path.exists(new_filepath)
        new_file_has_content = new_file_exists and os.path.isfile(new_filepath) and os.path.getsize(new_filepath) > 2
        old_file_exists_and_not_backed_up = os.path.exists(old_filepath) and not os.path.exists(old_filepath + ".bak")

        # --- Path 1: 尝试加载新日志文件 ---
        if new_file_has_content:
            try:
                with open(new_filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    current_failures_list = data
                    # v12.1.2: 执行“二次迁移”，为 v11.0.10 迁移的数据打上 "reason" 标记
                    for item in current_failures_list:
                         if isinstance(item, dict) and "reason" not in item:
                            item["reason"] = "MIGRATED_UNKNOWN" # 标记为迁移数据
                            needs_resave = True
                else:
                    logger.error(f"[Failure Log Load] {new_filepath} is not a list. Re-initializing.")
            except Exception as e:
                logger.error(f"[Failure Log Load] Error loading {new_filepath}: {e}. Attempting recovery.")

        # --- Path 2: 检查是否需要从旧日志迁移 ---
        if old_file_exists_and_not_backed_up:
            logger.warning(f"[Failure Log Load] Old log '{old_filepath}' still exists. Checking for merge/migration...")
            old_set = load_failed_submissions_old_format()
            
            if old_set:
                # 找出新日志中已有的表达式
                current_expressions = {item['expression'] for item in current_failures_list if isinstance(item, dict)}
                
                items_to_migrate = []
                for expr in old_set:
                    if expr not in current_expressions:
                        # v12.1.2: 迁移时添加 "reason" 标记
                        items_to_migrate.append({
                            "expression": expr, 
                            "reason": "MIGRATED_UNKNOWN", 
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                
                if items_to_migrate:
                    logger.warning(f"Found {len(items_to_migrate)} new entries in old log. Merging...")
                    current_failures_list.extend(items_to_migrate)
                    needs_resave = True # 标记需要回写
                
                # 无论是否合并了新条目，只要旧文件存在，就备份它
                try:
                    os.rename(old_filepath, old_filepath + ".bak")
                    logger.info(f"Old failure log backed up to {old_filepath}.bak.")
                except Exception as rename_e:
                    logger.error(f"Failed to rename old failure log: {rename_e}")
            else:
                logger.warning(f"[Failure Log Load] Old log '{old_filepath}' was loaded but was empty. Backing it up.")
                try:
                    os.rename(old_filepath, old_filepath + ".bak")
                except Exception as rename_e:
                    logger.error(f"Failed to rename old failure log: {rename_e}")

        # --- Path 3: 如果新文件仍然不存在 (全新安装) ---
        elif not new_file_exists:
             logger.warning(f"[Failure Log Load] No valid failure logs found. Creating new empty log file at {new_filepath}.")
             try:
                with open(new_filepath, 'w', encoding='utf-8') as f:
                    json.dump([], f) # 写入一个空的 JSON 列表
                logger.info(f"Successfully created new empty log file: {new_filepath}")
             except Exception as create_e:
                logger.error(f"Failed to create new empty log file! {create_e}", exc_info=True)

        # --- v12.1.2: 统一回写 ---
        if needs_resave:
            logger.info(f"[Failure Log Load] Resaving {new_filepath} with 'reason' flags...")
            try:
                with open(new_filepath, 'w', encoding='utf-8') as f:
                    json.dump(current_failures_list, f, indent=4)
                logger.info(f"Successfully migrated/updated {len(current_failures_list)} failure entries.")
            except Exception as save_e:
                logger.error(f"Failed to save migrated failure log (inline): {save_e}.")

        return current_failures_list

def save_submission_failures(failures_list):
    """ (v11.0.5) 保存新的 submission_failure_log.json """
    with failure_log_lock:
        filepath = SUBMISSION_FAILURE_LOG_FILE
        try:
            if not isinstance(failures_list, list):
                 logger.error(f"[Failure Log Save] Invalid data type: {type(failures_list)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(failures_list, f, indent=4)
            return True
        except Exception as e: logger.error(f"[Failure Log Save] Error saving {filepath}: {e}", exc_info=False); return False

def load_failed_submissions():
    """
    (v12.1.2) 兼容 v6.1.5 get_hopeful_alphas_stats。
    返回一个 dict map (v12.0.0 修改) 和 set
    """
    failures_list = load_submission_failures()
    
    # v12.0.0: 创建一个 Map 用于 O(1) 查找时间戳
    failed_map = {}
    for item in failures_list:
        if isinstance(item, dict) and 'expression' in item:
            # v12.1.2: 存储 'timestamp' 和 'reason'
            failed_map[item['expression']] = {
                "timestamp": item.get('timestamp', 'N/A'),
                "reason": item.get('reason', 'UNKNOWN')
            }
                  
    failed_set = set(failed_map.keys())
    
    return failed_map, failed_set
# --- v12.1.4: 结束 ---

def get_service_status(log_file):
    # 保持 v6.1.5 逻辑
    status = "UNKNOWN"; last_seen = "Never"; logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT: status = "RUNNING"
            else: status = "STALLED"
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                latest_lines = deque(f, maxlen=50)
            # --- [BUG 修复] ---
            # 原代码: logs = "".join(latest_lines) (顺序)
            # 新代码: 使用 reversed() 来实现倒序
            logs = "".join(reversed(latest_lines))
            # --- [修复结束] ---
        except Exception as e: logs = f"Error reading log: {e}"; status = "ERROR"; logger.error(f"Error status for {log_file}: {e}", exc_info=False)
    else: status = "NOT FOUND"
    return {"status": status, "last_seen": last_seen, "logs": logs}


def get_hopeful_alphas_stats():
    # v12.0.0: 修复统计 Bug
    stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try: 
        # --- BEGIN v12.0.0 (Task 1 & 2) ---
        submitted_dict = load_submitted_alphas() # v12.1.4: 包含 'reason'
        failed_map, failed_set = load_failed_submissions() # v12.1.4: 包含 'reason'
        # --- END v12.0.0 ---
        
        alphas = load_hopeful_alphas_list() # v11.0.11: 使用辅助函数

        if alphas:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list)

            # --- 保持 v6.1.5 的统计计算方式 ---
            all_fitness = [a.get('performance', {}).get('fitness') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]
            all_sharpe = [a.get('performance', {}).get('sharpe') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]

            valid_fitness = [float(f) for f in all_fitness if isinstance(f, (int, float, str)) and re.match(r'^-?\d+(\.\d+)?$', str(f))]
            valid_sharpe = [float(s) for s in all_sharpe if isinstance(s, (int, float, str)) and re.match(r'^-?\d+(\.\d+)?$', str(s))]

            if valid_fitness:
                 stats['max_fitness'] = max(valid_fitness) if valid_fitness else 0.0
                 stats['avg_fitness'] = sum(valid_fitness) / len(valid_fitness) if valid_fitness else 0.0
            if valid_sharpe:
                 stats['max_sharpe'] = max(valid_sharpe) if valid_sharpe else 0.0
            # --- 结束 v6.1.5 统计计算 ---

            # 保持 v6.1.5 的评分逻辑
            def calculate_dashboard_score(report):
                if not isinstance(report, dict): return -float('inf')
                perf = report.get('performance', {})
                if not isinstance(perf, dict): return -float('inf')
                fitness = perf.get('fitness', -999); sharpe = perf.get('sharpe', 0.0); turnover = perf.get('turnover', 1.0)
                checks_summary = report.get('checks_summary', '0 PASS'); passed_count = 0
                try:
                    match = re.compile(r'(\d+)\s+PASS').search(checks_summary or '')
                    if match: passed_count = int(match.group(1))
                except (ValueError, TypeError): pass
                try: fitness_f = float(fitness)
                except (ValueError, TypeError): fitness_f = -999
                try: sharpe_f = float(sharpe)
                except (ValueError, TypeError): sharpe_f = 0.0
                try: turnover_f = float(turnover)
                except (ValueError, TypeError): turnover_f = 1.0
                return fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1)


            processed_alphas_temp = []
            successfully_submitted_count_local = 0
            total_submitted_count_local = 0

            pass_pattern = re.compile(r'(\d+)\s+PASS') 
            fail_pattern = re.compile(r'(\d+)\s+FAIL') 

            for alpha_report in valid_alphas_list:
                try: 
                    expression = alpha_report.get('expression')
                    if not expression: continue
                    
                    is_failed_on_wq = expression in failed_set 
                    is_submitted = expression in submitted_dict # v12.0.0: 检查 dict keys

                    perf_data = alpha_report.get('performance', {});
                    if not isinstance(perf_data, dict): perf_data = {}
                    summary_str = alpha_report.get('checks_summary', '') or ''
                    fail_match = fail_pattern.search(summary_str);
                    has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
                    pass_match = pass_pattern.search(summary_str);
                    passed_count = int(pass_match.group(1)) if pass_match else 0
                    is_submittable = passed_count >= 7 and not has_fail
                    
                    is_successfully_submitted = is_submittable and is_submitted and not is_failed_on_wq

                    if is_submittable and not is_submitted and not is_failed_on_wq: 
                        stats['submittable_pending_count'] += 1

                    # --- BEGIN v12.0.0 BUG 修复 (Task 2) ---
                    # “总提交数”现在包括已提交的 和 已失败的
                    if is_submitted or is_failed_on_wq:
                         total_submitted_count_local += 1
                         if is_successfully_submitted: # 成功的逻辑不变
                             successfully_submitted_count_local += 1
                    # --- END v12.0.0 BUG 修复 ---

                    # --- BEGIN v12.0.0 (Task 1) ---
                    # 获取手动操作的时间戳
                    manual_timestamp = "N/A"
                    if is_submitted:
                        manual_timestamp = submitted_dict.get(expression, {}).get('manual_timestamp', 'N/A')
                    elif is_failed_on_wq:
                        manual_timestamp = failed_map.get(expression, {}).get('timestamp', 'N/A') # v12.1.2: 修复
                    # --- END v12.0.0 ---

                    processed_alpha_data = {
                        "expression": expression, 
                        "timestamp": alpha_report.get('timestamp', 'N/A'), # 这是 Alpha *生成* 时间戳
                        "manual_timestamp": manual_timestamp, # v12.0.0: 这是 *手动操作* 时间戳
                        "checks_summary": summary_str, 
                        "is_submittable": is_submittable,
                        "is_submitted": is_submitted, 
                        "is_failed_on_wq": is_failed_on_wq,
                        "is_successfully_submitted": is_successfully_submitted,
                        "dashboard_score": calculate_dashboard_score(alpha_report), 
                        "performance": perf_data
                    }
                    processed_alphas_temp.append(processed_alpha_data)
                except Exception as e: logger.error(f"[Stats Process Alpha] Error for {expression[:30]}...: {e}", exc_info=False)

            stats['all_alphas'] = processed_alphas_temp
            stats['successfully_submitted_count'] = successfully_submitted_count_local
            stats['total_submitted_count'] = total_submitted_count_local

    except Exception as e:
        logger.error(f"[Stats] CRITICAL Error in get_hopeful_alphas_stats: {e}", exc_info=True)
        # 返回默认空 stats
        stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
                  "submittable_pending_count": 0, "successfully_submitted_count": 0,
                  "total_submitted_count": 0, "all_alphas": [] }
    return stats


def get_version_from_file(file_path, version_regex_str):
    # 保持 v6.1.5 逻辑
    # logger.info(f"[Version] Reading {file_path}")
    version_regex = re.compile(version_regex_str)
    try:
        if not os.path.isfile(file_path): return "file_not_found"
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        match = version_regex.search(content)
        return match.group(1) if match else "unknown_format"
    except Exception as e: logger.error(f"[Version] Error reading {file_path}: {e}"); return "read_error"

# --- Routes ---
@app.route('/')
def dashboard():
    # v11.0.5: 指向修改后的 legacy 模板名
    return render_template('dashboard_v4_legacy.html', settings_page=True, chart_page=True)

# 其他路由 (/settings, /chart, /api/get_settings, /api/save_settings) 保持 v6.1.5 逻辑，仅调整日志和中文提示

@app.route('/settings')
def settings_page():
    # logger.info("[API /settings] Request received.")
    return render_template('settings.html')

@app.route('/chart')
def chart_page():
    # logger.info("[API /chart] Request received.")
    return render_template('chart.html')

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    # logger.info("[API /api/get_settings]")
    with config_lock:
        try:
            # 简化文件存在检查
            if not os.path.exists(SYSTEM_CONFIG_FILE):
                return jsonify({"error": "Config file not found."}), 404
            with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
            response = make_response(jsonify(data))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response
        except Exception as e:
            logger.error(f"[API /api/get_settings] Error: {e}", exc_info=False)
            return jsonify({"error": "读取配置文件时出错。"}), 500 # 中文提示

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    logger.info("[API /api/save_settings]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    new_config = request.json
    expected_keys = { "wq_api_cooldown": int, "llm_api_cooldown": int, "miner_concurrency": int,
                      "miner_sleep": int, "evolver_concurrency": int, "evolver_sleep": int,
                      "producer_queue_full_sleep": int }
    if not isinstance(new_config, dict): return jsonify(status='error', message='无效的 JSON 格式'), 400
    validated_config = {}
    with config_lock:
        try:
            if os.path.exists(SYSTEM_CONFIG_FILE):
                try:
                    with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: validated_config = json.load(f)
                except json.JSONDecodeError: validated_config = {}

            for key, expected_type in expected_keys.items():
                if key in new_config:
                    value = new_config[key]
                    try:
                        converted_value = expected_type(value)
                        if converted_value < 0: return jsonify(status='error', message=f"{key} 必须大于等于 0"), 400
                        validated_config[key] = converted_value
                    except (ValueError, TypeError):
                         return jsonify(status='error', message=f"{key} 的类型无效。"), 400
                elif key not in validated_config: # 确保所有预期的键都存在
                     return jsonify(status='error', message=f"缺少键: {key}"), 400

            validated_config['evolver_search_space'] = new_config.get('evolver_search_space', validated_config.get('evolver_search_space', {}))

            with open(SYSTEM_CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(validated_config, f, indent=2)
            logger.info(f"[API /api/save_settings] 配置已保存。")
            global _timeseries_cache, _timeseries_cache_time
            with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
            return jsonify(status='success', message='配置已保存') # 中文提示
        except Exception as e:
            logger.error(f"[API /api/save_settings] Error: {e}", exc_info=True)
            return jsonify(status='error', message="保存配置时发生内部错误。"), 500


@app.route('/status')
def status():
    # logger.info("[API /status]") # 减少日志
    try:
        # get_hopeful_alphas_stats 现在更健壮
        data = { "miner": get_service_status('miner.log'),
                 "evolver": get_service_status('evolver.log'),
                 "hopeful_alphas": get_hopeful_alphas_stats() }
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.critical(f"[API /status] CRITICAL Error: {e}", exc_info=True)
        # 返回包含错误的默认结构，防止前端JS完全失败
        error_data = {
             "miner": {"status": "ERROR", "last_seen": "N/A", "logs": f"获取状态时出错: {e}"},
             "evolver": {"status": "ERROR", "last_seen": "N/A", "logs": f"获取状态时出错: {e}"},
             "hopeful_alphas": { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
                                 "submittable_pending_count": 0, "successfully_submitted_count": 0,
                                 "total_submitted_count": 0, "all_alphas": [], "error": f"获取统计时出错: {e}" }
        }
        return jsonify(error_data), 500 # 仍然返回 500 错误码

@app.route('/api/version_info')
def version_info():
    # logger.info("[API /version_info]")
    dashboard_version = CURRENT_DASHBOARD_VERSION # 使用新版本号
    generator_version = get_version_from_file(GENERATOR_FILE_PATH, r'CURRENT_GENERATOR_VERSION\s*=\s*["\'](v[0-9]+\.[0-9]+\.[^"\']*)["\']')
    data = {"dashboard_version": dashboard_version, "generator_version": generator_version}
    response = make_response(jsonify(data)); response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; return response

@app.route('/api/v1/stats/timeseries')
def api_stats_timeseries():
    # 保持 v6.1.5 的实现
    global _timeseries_cache, _timeseries_cache_time
    with _cache_lock:
        now = datetime.now(timezone.utc)
        if _timeseries_cache and _timeseries_cache_time and (now - _timeseries_cache_time < CACHE_DURATION):
            return jsonify(_timeseries_cache)
        with tested_log_lock:
            if not os.path.exists(TESTED_ALPHAS_LOG_FILE):
                return jsonify({"error": "Log file not found."}), 404
            try:
                # logger.info("[API Timeseries] Reading log...")
                df = pd.read_json(TESTED_ALPHAS_LOG_FILE)
                # logger.info(f"[API Timeseries] Read {len(df)} entries.")
                if df.empty:
                    _timeseries_cache = {"timestamps": [], "count": [], "mean_fitness": [], "high_quality_count": []}
                    _timeseries_cache_time = now; return jsonify(_timeseries_cache)

                df['timestamp_dt'] = pd.to_datetime(df['timestamp'], errors='coerce')
                df = df.dropna(subset=['timestamp_dt'])
                df['fitness_num'] = pd.to_numeric(df['fitness'], errors='coerce')
                df_valid_fitness = df.dropna(subset=['fitness_num'])
                df['passed_checks_num'] = pd.to_numeric(df['passed_checks'], errors='coerce').fillna(0)
                df = df.set_index('timestamp_dt')
                df_valid_fitness = df_valid_fitness.set_index('timestamp_dt')

                resampler_all = df.resample('h')
                resampler_valid = df_valid_fitness.resample('h')

                agg_counts = resampler_all['expression'].size()
                agg_mean_fitness = resampler_valid['fitness_num'].mean()
                df_hq = df[(df['fitness_num'] > 0.5) & (df['passed_checks_num'] >= 4)]
                hq_counts = df_hq.resample('h').size() if not df_hq.empty else pd.Series(dtype=int)

                combined_index = agg_counts.index.union(agg_mean_fitness.index).union(hq_counts.index)
                output_df = pd.DataFrame(index=combined_index)
                output_df['count'] = agg_counts.reindex(combined_index, fill_value=0).astype(int)
                output_df['mean_fitness'] = agg_mean_fitness.reindex(combined_index)
                output_df['high_quality_count'] = hq_counts.reindex(combined_index, fill_value=0).astype(int)

                output = { "timestamps": output_df.index.strftime('%Y-%m-%dT%H:%M:%S').tolist(),
                           "count": output_df['count'].tolist(),
                           "mean_fitness": output_df['mean_fitness'].round(4).replace({np.nan: None}).tolist(),
                           "high_quality_count": output_df['high_quality_count'].tolist() }
                _timeseries_cache = output; _timeseries_cache_time = now
                # logger.info(f"[API Timeseries] Processed {len(df)} entries.")
                return jsonify(output)
            except Exception as e:
                logger.error(f"[API Timeseries] Error: {e}", exc_info=True)
                with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                return jsonify({"error": "内部服务器错误。"}), 500

# --- BEGIN v12.1.3: 修复图表“僵尸”统计 Bug ---
def get_daily_submission_stats():
    """ (v12.1.3) 聚合每日数据，修复 v12.1.2 迁移导致的 "0 提交" Bug """
    
    daily_submitted_counter = Counter()
    daily_failed_counter = Counter()
    
    # 1. 统计已提交 (过滤僵尸数据)
    submitted_dict = load_submitted_alphas() # v12.1.4: 这会正确地标记 "reason"
    for item in submitted_dict.values():
        reason = item.get("reason")
        # v12.1.3: *只* 统计 'MANUAL_ADD' (v12.1.2+ 手动添加)
        # (v12.1.4 迁移逻辑会将 v12.0.0-v12.1.1 的手动添加也标记为 'MANUAL_ADD')
        if isinstance(item, dict) and 'manual_timestamp' in item and reason == "MANUAL_ADD":
            try:
                ts_str = item['manual_timestamp']
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                daily_submitted_counter[dt.date()] += 1
            except (ValueError, TypeError):
                pass 
                
    # 2. 统计已失败 (过滤僵尸数据)
    failures_list = load_submission_failures() # v12.1.4: 这会正确地标记 "reason"
    for item in failures_list:
        reason = item.get("reason")
        # v12.1.3: *只* 统计*非* MIGRATED_UNKNOWN 的
        if isinstance(item, dict) and 'timestamp' in item and reason != "MIGRATED_UNKNOWN":
            try:
                ts_str = item['timestamp']
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                daily_failed_counter[dt.date()] += 1
            except (ValueError, TypeError):
                pass 

    # Chart B: 每日产出 (Hopeful) - 这个逻辑保持不变
    daily_hopeful_counter = Counter()
    hopeful_list = load_hopeful_alphas_list()
    for item in hopeful_list:
        if isinstance(item, dict) and 'timestamp' in item:
            try:
                dt = datetime.strptime(item['timestamp'], '%Y-%m-%d %H:%M:%S')
                daily_hopeful_counter[dt.date()] += 1
            except (ValueError, TypeError):
                pass 

    # 合并所有日期并排序
    all_dates = sorted(list(
        set(daily_submitted_counter.keys()) | 
        set(daily_failed_counter.keys()) | 
        set(daily_hopeful_counter.keys())
    ))
    
    # 格式化输出
    output = {
        "timestamps": [],
        "submitted_count": [],
        "failed_count": [],   
        "hopeful_count": []
    }
    
    if not all_dates:
        # v12.1.3: 即使没有数据，也返回今天
        today_str = datetime.now(timezone.utc).date().isoformat()
        output["timestamps"].append(today_str)
        output["submitted_count"].append(0)
        output["failed_count"].append(0)
        output["hopeful_count"].append(0)
        return output
        
    # 填充日期范围以确保连续性
    start_date = all_dates[0]
    end_date = max(all_dates[-1], datetime.now(timezone.utc).date())
    date_range = pd.date_range(start=start_date, end=end_date, freq='D')
    
    for date_obj in date_range:
        date_key = date_obj.date()
        output["timestamps"].append(date_key.isoformat())
        output["submitted_count"].append(daily_submitted_counter.get(date_key, 0))
        output["failed_count"].append(daily_failed_counter.get(date_key, 0))
        output["hopeful_count"].append(daily_hopeful_counter.get(date_key, 0))

    return output


@app.route('/api/v1/stats/submission_daily')
def api_stats_submission_daily():
    """ (v12.1.0) 任务 3 - 新的每日图表 API """
    global _submission_cache, _submission_cache_time
    with _submission_cache_lock:
        now = datetime.now(timezone.utc)
        # 使用与主 API 相同的缓存时间
        if _submission_cache and _submission_cache_time and (now - _submission_cache_time < CACHE_DURATION):
            return jsonify(_submission_cache)
        
        try:
            # logger.info("[API Daily Stats] Generating daily stats...")
            stats = get_daily_submission_stats() # v12.1.3: 调用已修复的函数
            _submission_cache = stats
            _submission_cache_time = now
            # logger.info("[API Daily Stats] Daily stats cached.")
            return jsonify(stats)
        except Exception as e:
            logger.error(f"[API Daily Stats] Error: {e}", exc_info=True)
            with _submission_cache_lock:
                _submission_cache = None
                _submission_cache_time = None
            return jsonify({"error": "内部服务器错误。"}), 500
# --- END v12.1.3 ---


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    # 保持 v6.1.5 允许的文件列表
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files: return "无效的日志文件请求", 404
    try: return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except Exception as e: logger.error(f"[API /download_logs] Error: {e}"); return "下载文件时出错", 500

# --- v12.1.2: 更新 Mark/Unmark APIs (添加 'reason' 标记) ---
@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    operation = "Mark"; # logger.info(f"[API /{operation.lower()}_submitted]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_dict = load_submitted_alphas() # v12.1.4: 加载 (并自动迁移)
        # v12.1.2: 写入 dict, 添加时间戳 和 *新的* "reason" 标记
        submitted_dict[expression] = {
            "manual_timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "MANUAL_ADD" 
        }
        
        if save_submitted_alphas(submitted_dict): 
            return jsonify(status='success', message='标记成功')
        else: 
            logger.error(f"[API /{operation.lower()}_submitted] Save failed."); 
            return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: 
        logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); 
        return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    operation = "Unmark"; # logger.info(f"[API /{operation.lower()}_submitted]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_dict = load_submitted_alphas() # v12.1.4: 加载 (并自动迁移)
        submitted_dict.pop(expression, None) 
        
        if save_submitted_alphas(submitted_dict): 
            return jsonify(status='success', message='取消标记成功')
        else: 
            logger.error(f"[API /{operation.lower()}_submitted] Save failed."); 
            return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: 
        logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); 
        return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/mark_failed_on_wq', methods=['POST'])
def mark_alpha_failed():
    operation = "MarkFailed"; logger.info(f"[API /{operation.lower()}]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json
    expression = data.get('expression')
    reason = data.get('reason')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    if not reason: reason = "UNKNOWN_REASON" # 默认原因
    logger.info(f"[API /{operation.lower()}] Expr: {expression[:50]}... Reason: {reason}")
    try:
        failures_list = load_submission_failures() # v12.1.4: 加载 (并自动迁移)
        found = False
        for item in failures_list:
            if isinstance(item, dict) and item.get('expression') == expression:
                item['reason'] = reason; item['timestamp'] = datetime.now(timezone.utc).isoformat()
                found = True; break
        
        if not found:
            # v12.1.2: 写入时添加 'reason' (来自用户)
            failures_list.append({ 
                "expression": expression, 
                "reason": reason, 
                "timestamp": datetime.now(timezone.utc).isoformat() 
            })
        
        if save_submission_failures(failures_list): # 保存新日志
            
            # --- BEGIN v12.0.0 (Task 1) 二次检查 (使用新数据结构) ---
            try:
                logger.info(f"[API /{operation.lower()}] 正在执行二次检查... 从 'submitted_alphas.json' 中移除...")
                submitted_dict = load_submitted_alphas() # v12.1.4: 加载 (并自动迁移)
                if expression in submitted_dict:
                    submitted_dict.pop(expression, None) 
                    if not save_submitted_alphas(submitted_dict): 
                         logger.error(f"[API /{operation.lower()}] 二次检查：保存 submitted_alphas 失败。")
                    else:
                         logger.info(f"[API /{operation.lower()}] 二次检查：成功从 submitted_alphas 中移除。")
            except Exception as e_secondary:
                logger.error(f"[API /{operation.lower()}] 二次检查时发生意外错误: {e_secondary}")
            # --- END v12.0.0 ---

            # --- v11.0.12: 移除了 v11.0.11 的“三次检查” (干掉 Hopeful Pool 的 Bug) ---
            
            return jsonify(status='success', message=f'已标记失败 (原因: {reason})')
        else: 
            logger.error(f"[API /{operation.lower()}] Save failed."); 
            return jsonify(status='error', message='保存失败日志失败'), 500
        # --- [修复结束] ---

    except Exception as e: 
        logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); 
        return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_failed_on_wq', methods=['POST'])
def unmark_alpha_failed():
    operation = "UnmarkFailed"; logger.info(f"[API /{operation.lower()}]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        failures_list = load_submission_failures() # 安全读取
        original_size = len(failures_list)
        new_failures_list = [ item for item in failures_list if not (isinstance(item, dict) and item.get('expression') == expression) ]
        new_size = len(new_failures_list)
        if original_size == new_size: logger.warning(f"[API /{operation.lower()}] Expression not found.")
        if save_submission_failures(new_failures_list): # 安全保存
            return jsonify(status='success', message='已取消标记失败')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='保存失败日志失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500
# --- v12.1.4: 结束 ---
# --- BEGIN: 新增代码 (for /pending page) ---
@app.route('/pending')
def pending_page():
    """ 渲染待提交Alphas的专属页面 """
    logger.info("[API /pending] Rendering pending alphas page.")
    # 渲染一个新的HTML模板
    return render_template('pending.html')

@app.route('/api/get_pending_alphas')
def get_pending_alphas():
    """ 
    提供一个专门的API，仅返回待提交的Alphas列表。
    逻辑: is_submittable=True, is_submitted=False, is_failed_on_wq=False
    """
    # logger.info("[API /api/get_pending_alphas] Request received.") # 减少日志
    try:
        # 调用现有的统计函数
        stats = get_hopeful_alphas_stats()
        all_alphas = stats.get('all_alphas', [])
        
        # v12.0.0: 这里的过滤逻辑现在依赖于 get_hopeful_alphas_stats() 
        # (该函数现在会正确地*处理*所有 Alpha)
        pending_alphas = []
        for alpha in all_alphas:
            # 过滤条件 (与 get_hopeful_alphas_stats 中 'submittable_pending_count' 的逻辑一致)
            if (alpha.get('is_submittable') and 
                not alpha.get('is_submitted') and 
                not alpha.get('is_failed_on_wq')):
                
                # 只提取需要的信息，减小包大小
                alpha_perf = alpha.get('performance', {})
                if not isinstance(alpha_perf, dict): alpha_perf = {} # 确保是字典
                
                pending_alphas.append({
                    "expression": alpha.get('expression'),
                    "fitness": alpha_perf.get('fitness'),
                    "sharpe": alpha_perf.get('sharpe'),
                    "turnover": alpha_perf.get('turnover'),
                    "returns": alpha_perf.get('returns'),
                    "checks_summary": alpha.get('checks_summary'),
                    "dashboard_score": alpha.get('dashboard_score'),
                    "timestamp": alpha.get('timestamp')
                    # v12.0.0: manual_timestamp 暂时不在 pending 页面显示
                })
        
        # logger.info(f"[API /api/get_pending_alphas] Found {len(pending_alphas)} pending alphas.")
        
        # 按 dashboard_score 降序排序
        pending_alphas.sort(key=lambda x: x.get('dashboard_score', -999.0) or -999.0, reverse=True)
        
        response = make_response(jsonify(pending_alphas))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; 
        response.headers['Pragma'] = 'no-cache'; 
        response.headers['Expires'] = '0'
        return response
        
    except Exception as e:
        logger.critical(f"[API /api/get_pending_alphas] CRITICAL Error: {e}", exc_info=True)
        return jsonify({"error": f"Failed to get pending alphas: {e}"}), 500
# --- END: 新增代码 ---

if __name__ == '__main__':
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR); logger.info(f"Created log directory: {LOG_DIR}")
        except OSError as e: logger.error(f"Error creating log directory {LOG_DIR}: {e}")
    # 移除 os.stat_cache()
    logger.info(f"Starting Flask application (Version: {CURRENT_DASHBOARD_VERSION})...")
    try:
        import pandas
        logger.info(f"Pandas version {pd.__version__} detected.")
    except ImportError:
         logger.warning("Pandas library not found. Timeseries API may not work.")

    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)