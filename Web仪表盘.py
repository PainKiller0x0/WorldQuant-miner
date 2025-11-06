# --- Web仪表盘.py v13.3.7 (状态文件安全修复) ---
# (此版本与 v13.3.5 相同, 重新应用以修复格式战 Bug)
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from collections import deque, Counter
import os.path
import logging
import pandas as pd
import numpy as np
import time 

# --- v13.3.5: 强制使用 utils 中的安全函数 ---
# 导入我们 v13.3.6 版的、带保险的 utils
import utils
# --- v13.3.5: 结束 ---

# --- v13.3.7: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v13.3.7 (State Safety Fix)"
# --- v13.3.7: 结束 ---

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
# --- v13.3.5: 移除不安全的本地路径和锁 (已集中到 utils.py) ---
# HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json') # <--- 移除
# hopeful_lock = threading.Lock() # <--- 移除
# --- v13.3.5: 结束 ---

SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
SUBMISSION_FAILURE_LOG_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submission_failure_log.json')) 
FAILED_SUBMISSIONS_FILE_OLD = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json')) 
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
DASHBOARD_FILE_PATH = os.path.join(BASE_DIR, "Web仪表盘.py")
TESTED_ALPHAS_LOG_FILE = os.path.join(BASE_DIR, 'tested_alphas_log.json')

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
CACHE_DURATION = timedelta(seconds=300) 

file_lock = threading.Lock()
# hopeful_lock = threading.Lock() # v13.3.5: 移除
failure_log_lock = threading.Lock() 
tested_log_lock = threading.Lock()

_timeseries_cache = None
_timeseries_cache_time = None
_cache_lock = threading.Lock()

_submission_cache = None
_submission_cache_time = None
_submission_cache_lock = threading.Lock()

# (v13.3.5: 我们暂时保留这些不安全的函数，只修复 hopeful_alphas)
def load_submitted_alphas():
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): return {}
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return {}
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            final_dict = {}; needs_resave = False; migration_timestamp = None
            if isinstance(data, (list, set)):
                logger.warning(f"[Submit Load] Old data format (list) detected in {filepath}. Migrating...")
                migration_timestamp = datetime.now(timezone.utc).isoformat()
                for expr in data:
                    if isinstance(expr, str):
                        final_dict[expr] = {"manual_timestamp": migration_timestamp, "reason": "MIGRATED_UNKNOWN"}
                needs_resave = True
            elif isinstance(data, dict):
                final_dict = data
                timestamps_no_reason = [ item.get('manual_timestamp') for item in final_dict.values() if isinstance(item, dict) and "reason" not in item ]
                if timestamps_no_reason:
                    zombie_timestamp = Counter(timestamps_no_reason).most_common(1)[0][0]
                    logger.warning(f"[Submit Load] Found v12.0.0 migrated data (Timestamp: {zombie_timestamp}). Upgrading to v12.1.4 flags...")
                    needs_resave = True
                    for item in final_dict.values():
                        if isinstance(item, dict) and "reason" not in item:
                            if item.get('manual_timestamp') == zombie_timestamp: item["reason"] = "MIGRATED_UNKNOWN"
                            else: item["reason"] = "MANUAL_ADD"
            else:
                logger.warning(f"[Submit Load] File {filepath} bad format (not dict or list)."); return {}
            if needs_resave:
                logger.info(f"[Submit Load] Resaving {filepath} with new 'reason' flags...")
                try:
                    with open(filepath, 'w', encoding='utf-8') as f_save: json.dump(final_dict, f_save, indent=4)
                    logger.info(f"Successfully migrated/updated {len(final_dict)} entries with 'reason' flags.")
                except Exception as save_e: logger.error(f"Failed to save migrated data for {filepath}! {save_e}")
            return final_dict
        except Exception as e: logger.error(f"[Submit Load] Error loading {filepath}: {e}", exc_info=False); return {}

def save_submitted_alphas(submitted_dict):
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        try:
            if not isinstance(submitted_dict, dict): logger.error(f"[Submit Save] Invalid data type: {type(submitted_dict)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(submitted_dict, f, indent=4)
            return True
        except Exception as e: logger.error(f"[Submit Save] Error saving {filepath}: {e}", exc_info=False); return False

# --- v13.3.5: 移除不安全的 hopeful_alphas 读写函数 ---
# def load_hopeful_alphas_list(): # <--- 移除 (这是 Bug 的根源)
#     with hopeful_lock:
#         filepath = HOPEFUL_ALPHAS_FILE
#         if not (os.path.exists(filepath) and os.path.isfile(filepath) and os.path.getsize(filepath) > 2): return []
#         try:
#             with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
#             if isinstance(data, list): return data # <--- 这一行导致了 Bug
#             else: logger.warning(f"[Hopeful Load] {filepath} is not a list. Returning empty."); return [] # <--- 触发了
#         except Exception as e: logger.error(f"[Hopeful Load] Error loading {filepath}: {e}", exc_info=False); return []
#
# def save_hopeful_alphas_list(alphas_list): # <--- 移除 (这个函数没被使用)
#     with hopeful_lock:
#         filepath = HOPEFUL_ALPHAS_FILE
#         try:
#             if not isinstance(alphas_list, list): logger.error(f"[Hopeful Save] Invalid data type: {type(alphas_list)}."); return False
#             with open(filepath, 'w', encoding='utf-8') as f: json.dump(alphas_list, f, indent=4)
#             return True
#         except Exception as e: logger.error(f"[Hopeful Save] Error saving {filepath}: {e}", exc_info=False); return False
# --- v13.3.5: 结束 ---


def load_failed_submissions_old_format():
    filepath = FAILED_SUBMISSIONS_FILE_OLD
    if not (os.path.exists(filepath) and os.path.isfile(filepath)): return set()
    try:
        if os.path.getsize(filepath) < 2: return set()
        with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
        if isinstance(data, (list, set)): return set(data)
        else: logger.warning(f"[Old Failed Load] File {filepath} not list/set."); return set()
    except Exception as e: logger.error(f"[Old Failed Load] Error loading {filepath}: {e}"); return set()

def load_submission_failures():
    with failure_log_lock:
        new_filepath = SUBMISSION_FAILURE_LOG_FILE; old_filepath = FAILED_SUBMISSIONS_FILE_OLD
        current_failures_list = []; needs_resave = False
        new_file_exists = os.path.exists(new_filepath)
        new_file_has_content = new_file_exists and os.path.isfile(new_filepath) and os.path.getsize(new_filepath) > 2
        old_file_exists_and_not_backed_up = os.path.exists(old_filepath) and not os.path.exists(old_filepath + ".bak")
        if new_file_has_content:
            try:
                with open(new_filepath, 'r', encoding='utf-8') as f: data = json.load(f)
                if isinstance(data, list):
                    current_failures_list = data
                    for item in current_failures_list:
                         if isinstance(item, dict) and "reason" not in item:
                            item["reason"] = "MIGRATED_UNKNOWN"; needs_resave = True
                else: logger.error(f"[Failure Log Load] {new_filepath} is not a list. Re-initializing.")
            except Exception as e: logger.error(f"[Failure Log Load] Error loading {new_filepath}: {e}. Attempting recovery.")
        if old_file_exists_and_not_backed_up:
            logger.warning(f"[Failure Log Load] Old log '{old_filepath}' still exists. Checking for merge/migration...")
            old_set = load_failed_submissions_old_format()
            if old_set:
                current_expressions = {item['expression'] for item in current_failures_list if isinstance(item, dict)}
                items_to_migrate = []
                for expr in old_set:
                    if expr not in current_expressions:
                        items_to_migrate.append({ "expression": expr, "reason": "MIGRATED_UNKNOWN", "timestamp": datetime.now(timezone.utc).isoformat() })
                if items_to_migrate:
                    logger.warning(f"Found {len(items_to_migrate)} new entries in old log. Merging...")
                    current_failures_list.extend(items_to_migrate); needs_resave = True
                try: os.rename(old_filepath, old_filepath + ".bak"); logger.info(f"Old failure log backed up to {old_filepath}.bak.")
                except Exception as rename_e: logger.error(f"Failed to rename old failure log: {rename_e}")
            else:
                logger.warning(f"[Failure Log Load] Old log '{old_filepath}' was loaded but was empty. Backing it up.")
                try: os.rename(old_filepath, old_filepath + ".bak")
                except Exception as rename_e: logger.error(f"Failed to rename old failure log: {rename_e}")
        elif not new_file_exists:
             logger.warning(f"[Failure Log Load] No valid failure logs found. Creating new empty log file at {new_filepath}.")
             try:
                with open(new_filepath, 'w', encoding='utf-8') as f: json.dump([], f)
                logger.info(f"Successfully created new empty log file: {new_filepath}")
             except Exception as create_e: logger.error(f"Failed to create new empty log file! {create_e}", exc_info=True)
        if needs_resave:
            logger.info(f"[Failure Log Load] Resaving {new_filepath} with 'reason' flags...")
            try:
                with open(new_filepath, 'w', encoding='utf-8') as f: json.dump(current_failures_list, f, indent=4)
                logger.info(f"Successfully migrated/updated {len(current_failures_list)} failure entries.")
            except Exception as save_e: logger.error(f"Failed to save migrated failure log (inline): {save_e}.")
        return current_failures_list

def save_submission_failures(failures_list):
    with failure_log_lock:
        filepath = SUBMISSION_FAILURE_LOG_FILE
        try:
            if not isinstance(failures_list, list): logger.error(f"[Failure Log Save] Invalid data type: {type(failures_list)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(failures_list, f, indent=4)
            return True
        except Exception as e: logger.error(f"[Failure Log Save] Error saving {filepath}: {e}", exc_info=False); return False

def load_failed_submissions():
    failures_list = load_submission_failures()
    failed_map = {}
    for item in failures_list:
        if isinstance(item, dict) and 'expression' in item:
            failed_map[item['expression']] = { "timestamp": item.get('timestamp', 'N/A'), "reason": item.get('reason', 'UNKNOWN') }
    failed_set = set(failed_map.keys())
    return failed_map, failed_set

def get_service_status(log_file):
    status = "UNKNOWN"; last_seen = "Never"; logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT: status = "RUNNING"
            else: status = "STALLED"
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: latest_lines = deque(f, maxlen=50)
            logs = "".join(reversed(latest_lines))
        except Exception as e: logs = f"Error reading log: {e}"; status = "ERROR"; logger.error(f"Error status for {log_file}: {e}", exc_info=False)
    else: status = "NOT FOUND"
    return {"status": status, "last_seen": last_seen, "logs": logs}


# --- v13.3.7: 修复 get_hopeful_alphas_stats (使用安全加载) ---
def get_hopeful_alphas_stats():
    stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try: 
        submitted_dict = load_submitted_alphas() 
        failed_map, failed_set = load_failed_submissions() 
        
        # --- v13.3.7: 关键修复 ---
        # 替换不安全的本地函数
        # alphas = load_hopeful_alphas_list() # <--- 移除 (不安全, 导致 Bug)
        alphas = utils.load_hopeful_alphas_safe() # <--- 替换 (安全, 兼容 list 和 dict)
        # --- v13.3.7: 结束 ---

        total_submitted_count_local = len(submitted_dict) + len(failed_set)
        successfully_submitted_count_local = 0 

        if alphas:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list)

            all_fitness = [a.get('performance', {}).get('fitness') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]
            all_sharpe = [a.get('performance', {}).get('sharpe') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]
            valid_fitness = [float(f) for f in all_fitness if isinstance(f, (int, float, str)) and re.match(r'^-?\d+(\.\d+)?$', str(f))]
            valid_sharpe = [float(s) for s in all_sharpe if isinstance(s, (int, float, str)) and re.match(r'^-?\d+(\.\d+)?$', str(s))]
            if valid_fitness:
                 stats['max_fitness'] = max(valid_fitness) if valid_fitness else 0.0
                 stats['avg_fitness'] = sum(valid_fitness) / len(valid_fitness) if valid_fitness else 0.0
            if valid_sharpe:
                 stats['max_sharpe'] = max(valid_sharpe) if valid_sharpe else 0.0
            
            def calculate_dashboard_score(report):
                if not isinstance(report, dict): return -float('inf')
                perf = report.get('performance', {});
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
            
            pass_pattern = re.compile(r'(\d+)\s+PASS') 
            fail_pattern = re.compile(r'(\d+)\s+FAIL') 

            for alpha_report in valid_alphas_list:
                try: 
                    expression = alpha_report.get('expression')
                    if not expression: continue
                    
                    is_failed_on_wq = expression in failed_set 
                    is_submitted = expression in submitted_dict 

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

                    if is_successfully_submitted:
                        successfully_submitted_count_local += 1
                    
                    manual_timestamp = "N/A"
                    if is_submitted:
                        manual_timestamp = submitted_dict.get(expression, {}).get('manual_timestamp', 'N/A')
                    elif is_failed_on_wq:
                        manual_timestamp = failed_map.get(expression, {}).get('timestamp', 'N/A') 

                    processed_alpha_data = {
                        "expression": expression, 
                        "timestamp": alpha_report.get('timestamp', 'N/A'),
                        "manual_timestamp": manual_timestamp, 
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
        stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
                  "submittable_pending_count": 0, "successfully_submitted_count": 0,
                  "total_submitted_count": 0, "all_alphas": [] }
    return stats
# --- v13.3.7: 结束 ---

def get_version_from_file(file_path, version_regex_str):
    version_regex = re.compile(version_regex_str)
    try:
        if not os.path.isfile(file_path): return "file_not_found"
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        match = version_regex.search(content)
        return match.group(1) if match else "unknown_format"
    except Exception as e: logger.error(f"[Version] Error reading {file_path}: {e}"); return "read_error"

@app.route('/')
def dashboard():
    return render_template('dashboard_v4_legacy.html', settings_page=True, chart_page=True)

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/chart')
def chart_page():
    return render_template('chart.html')

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    try:
        # v13.3.7: 确保使用 utils 中的安全函数
        data = utils.load_system_config() 
        if not data:
            return jsonify({"error": "Config file not found or empty."}), 404
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except Exception as e:
        logger.error(f"[API /api/get_settings] Error: {e}", exc_info=True)
        return jsonify({"error": "读取配置文件时出错。"}), 500

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    logger.info("[API /api/save_settings] (v13.1)")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    new_data = request.json
    if not isinstance(new_data, dict): return jsonify(status='error', message='无效的 JSON 格式'), 400

    try:
        # v13.3.7: 确保使用 utils 中的安全函数
        current_config = utils.load_system_config()
        static_keys = ['miner_concurrency', 'evolver_concurrency', 'producer_queue_full_sleep', 'hopeful_pool_max_size']
        llm_keys = ['daily_budget_limit']
        wq_keys = ['wq_429_cooldown_seconds', 'max_tpm_limit', 'min_tpm_limit']
        
        for key in static_keys:
            if key in new_data:
                try: current_config[key] = int(new_data[key])
                except (ValueError, TypeError): return jsonify(status='error', message=f"无效的 {key} (必须是整数)"), 400
        
        if 'llm_budget' in new_data and isinstance(new_data['llm_budget'], dict):
            if 'llm_budget' not in current_config: current_config['llm_budget'] = {}
            for key in llm_keys:
                if key in new_data['llm_budget']:
                    try: current_config['llm_budget'][key] = int(new_data['llm_budget'][key])
                    except (ValueError, TypeError): return jsonify(status='error', message=f"无效的 {key} (必须是整数)"), 400

        if 'wq_api_limiter' in new_data and isinstance(new_data['wq_api_limiter'], dict):
            if 'wq_api_limiter' not in current_config: current_config['wq_api_limiter'] = {}
            for key in wq_keys:
                 if key in new_data['wq_api_limiter']:
                    try: current_config['wq_api_limiter'][key] = int(new_data['wq_api_limiter'][key])
                    except (ValueError, TypeError): return jsonify(status='error', message=f"无效的 {key} (必须是整数)"), 400

        if 'evolver_search_space' in new_data:
            current_config['evolver_search_space'] = new_data.get('evolver_search_space', current_config.get('evolver_search_space', {}))

        # v13.3.7: 确保使用 utils 中的安全函数
        if utils.save_system_config(current_config):
            logger.info(f"[API /api/save_settings] v13.1 配置已保存。")
            global _timeseries_cache, _timeseries_cache_time
            with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
            return jsonify(status='success', message='配置已保存')
        else:
            logger.error(f"[API /api/save_settings] v13.1 保存失败。")
            return jsonify(status='error', message='保存配置时发生内部错误。'), 500
    except Exception as e:
        logger.error(f"[API /api/save_settings] Error: {e}", exc_info=True)
        return jsonify(status='error', message="保存配置时发生内部错误。"), 500

@app.route('/status')
def status():
    try:
        # --- v13.3.7: 现在调用的是修复后的函数 ---
        data = { "miner": get_service_status('miner.log'),
                 "evolver": get_service_status('evolver.log'),
                 "hopeful_alphas": get_hopeful_alphas_stats() } 
        # --- v13.3.7: 结束 ---
                 
        try:
            # v13.3.7: 确保使用 utils 中的安全函数
            config = utils.load_system_config()
            llm_budget = config.get("llm_budget", {})
            wq_limiter = config.get("wq_api_limiter", {})
            
            wq_cooldown_status = "OK"; wq_cooldown_remaining = 0
            last_failure = wq_limiter.get("last_failure_timestamp", 0)
            cooldown_period = wq_limiter.get("wq_429_cooldown_seconds", 60)
            now = time.time()
            
            if now - last_failure < cooldown_period:
                wq_cooldown_remaining = round(cooldown_period - (now - last_failure))
                wq_cooldown_status = f"IN_COOLDOWN ({wq_cooldown_remaining}s)"

            data["watchdog_status"] = {
                "llm_budget_used": llm_budget.get("budget_used_today", 0),
                "llm_budget_limit": llm_budget.get("daily_budget_limit", 2000),
                "llm_budget_date_utc": llm_budget.get("budget_last_used_date_utc", "N/A"),
                "wq_current_tpm_limit": wq_limiter.get("current_tpm_limit", "N/A"),
                "wq_cooldown_status": wq_cooldown_status,
                "wq_cooldown_remaining_sec": wq_cooldown_remaining
            }
        except Exception as wd_e:
             logger.error(f"[API /status] 获取看门狗状态时出错: {wd_e}", exc_info=False)
             data["watchdog_status"] = {"error": str(wd_e)}
            
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.critical(f"[API /status] CRITICAL Error: {e}", exc_info=True)
        error_data = {
             "miner": {"status": "ERROR", "last_seen": "N/A", "logs": f"获取状态时出错: {e}"},
             "evolver": {"status": "ERROR", "last_seen": "N/A", "logs": f"获取状态时出错: {e}"},
             "hopeful_alphas": { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0, "submittable_pending_count": 0, "successfully_submitted_count": 0, "total_submitted_count": 0, "all_alphas": [], "error": f"获取统计时出错: {e}" },
             "watchdog_status": {"error": f"获取状态时出错: {e}"} 
        }
        return jsonify(error_data), 500

@app.route('/api/version_info')
def version_info():
    dashboard_version = CURRENT_DASHBOARD_VERSION 
    generator_version = get_version_from_file(GENERATOR_FILE_PATH, r'CURRENT_GENERATOR_VERSION\s*=\s*["\'](v[0-9]+\.[0-9]+\.[^"\']*)["\']')
    data = {"dashboard_version": dashboard_version, "generator_version": generator_version}
    response = make_response(jsonify(data)); response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; return response

@app.route('/api/v1/stats/timeseries')
def api_stats_timeseries():
    global _timeseries_cache, _timeseries_cache_time
    with _cache_lock:
        now = datetime.now(timezone.utc)
        if _timeseries_cache and _timeseries_cache_time and (now - _timeseries_cache_time < CACHE_DURATION):
            return jsonify(_timeseries_cache)
        with tested_log_lock:
            if not os.path.exists(TESTED_ALPHAS_LOG_FILE): return jsonify({"error": "Log file not found."}), 404
            try:
                df = pd.read_json(TESTED_ALPHAS_LOG_FILE)
                if df.empty:
                    _timeseries_cache = {"timestamps": [], "count": [], "mean_fitness": [], "high_quality_count": []}
                    _timeseries_cache_time = now; return jsonify(_timeseries_cache)
                df['timestamp_dt'] = pd.to_datetime(df['timestamp'], errors='coerce'); df = df.dropna(subset=['timestamp_dt'])
                df['fitness_num'] = pd.to_numeric(df['fitness'], errors='coerce'); df_valid_fitness = df.dropna(subset=['fitness_num'])
                df['passed_checks_num'] = pd.to_numeric(df['passed_checks'], errors='coerce').fillna(0)
                df = df.set_index('timestamp_dt'); df_valid_fitness = df_valid_fitness.set_index('timestamp_dt')
                resampler_all = df.resample('h'); resampler_valid = df_valid_fitness.resample('h')
                agg_counts = resampler_all['expression'].size(); agg_mean_fitness = resampler_valid['fitness_num'].mean()
                df_hq = df[(df['fitness_num'] > 0.5) & (df['passed_checks_num'] >= 4)]
                hq_counts = df_hq.resample('h').size() if not df_hq.empty else pd.Series(dtype=int)
                combined_index = agg_counts.index.union(agg_mean_fitness.index).union(hq_counts.index)
                output_df = pd.DataFrame(index=combined_index)
                output_df['count'] = agg_counts.reindex(combined_index, fill_value=0).astype(int)
                output_df['mean_fitness'] = agg_mean_fitness.reindex(combined_index)
                output_df['high_quality_count'] = hq_counts.reindex(combined_index, fill_value=0).astype(int)
                output = { "timestamps": output_df.index.strftime('%Y-%m-%dT%H:%M:%S').tolist(), "count": output_df['count'].tolist(), "mean_fitness": output_df['mean_fitness'].round(4).replace({np.nan: None}).tolist(), "high_quality_count": output_df['high_quality_count'].tolist() }
                _timeseries_cache = output; _timeseries_cache_time = now
                return jsonify(output)
            except Exception as e:
                logger.error(f"[API Timeseries] Error: {e}", exc_info=True)
                with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                return jsonify({"error": "内部服务器错误。"}), 500

def get_daily_submission_stats():
    daily_submitted_counter = Counter(); daily_failed_counter = Counter()
    submitted_dict = load_submitted_alphas()
    for item in submitted_dict.values():
        reason = item.get("reason")
        if isinstance(item, dict) and 'manual_timestamp' in item and reason == "MANUAL_ADD":
            try:
                ts_str = item['manual_timestamp']; dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                daily_submitted_counter[dt.date()] += 1
            except (ValueError, TypeError): pass 
    failures_list = load_submission_failures()
    for item in failures_list:
        reason = item.get("reason")
        if isinstance(item, dict) and 'timestamp' in item and reason != "MIGRATED_UNKNOWN":
            try:
                ts_str = item['timestamp']; dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                daily_failed_counter[dt.date()] += 1
            except (ValueError, TypeError): pass 
    daily_hopeful_counter = Counter()
    
    # --- v13.3.7: 关键修复 ---
    # hopeful_list = load_hopeful_alphas_list() # <--- 移除 (不安全)
    hopeful_list = utils.load_hopeful_alphas_safe() # <--- 替换 (安全)
    # --- v13.3.7: 结束 ---

    for item in hopeful_list:
        if isinstance(item, dict) and 'timestamp' in item:
            try:
                dt = datetime.strptime(item['timestamp'], '%Y-%m-%d %H:%M:%S')
                daily_hopeful_counter[dt.date()] += 1
            except (ValueError, TypeError): pass 
    all_dates = sorted(list( set(daily_submitted_counter.keys()) | set(daily_failed_counter.keys()) | set(daily_hopeful_counter.keys()) ))
    output = { "timestamps": [], "submitted_count": [], "failed_count": [], "hopeful_count": [] }
    if not all_dates:
        today_str = datetime.now(timezone.utc).date().isoformat()
        output["timestamps"].append(today_str); output["submitted_count"].append(0); output["failed_count"].append(0); output["hopeful_count"].append(0)
        return output
    start_date = all_dates[0]; end_date = max(all_dates[-1], datetime.now(timezone.utc).date())
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
    global _submission_cache, _submission_cache_time
    with _submission_cache_lock:
        now = datetime.now(timezone.utc)
        if _submission_cache and _submission_cache_time and (now - _submission_cache_time < CACHE_DURATION):
            return jsonify(_submission_cache)
        try:
            stats = get_daily_submission_stats() 
            _submission_cache = stats; _submission_cache_time = now
            return jsonify(stats)
        except Exception as e:
            logger.error(f"[API Daily Stats] Error: {e}", exc_info=True)
            with _submission_cache_lock: _submission_cache = None; _submission_cache_time = None
            return jsonify({"error": "内部服务器错误。"}), 500

@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files: return "无效的日志文件请求", 404
    try: return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except Exception as e: logger.error(f"[API /download_logs] Error: {e}"); return "下载文件时出错", 500

@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    operation = "Mark";
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_dict = load_submitted_alphas()
        submitted_dict[expression] = { "manual_timestamp": datetime.now(timezone.utc).isoformat(), "reason": "MANUAL_ADD" }
        if save_submitted_alphas(submitted_dict): return jsonify(status='success', message='标记成功')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    operation = "Unmark";
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_dict = load_submitted_alphas()
        submitted_dict.pop(expression, None) 
        if save_submitted_alphas(submitted_dict): return jsonify(status='success', message='取消标记成功')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/mark_failed_on_wq', methods=['POST'])
def mark_alpha_failed():
    operation = "MarkFailed"; logger.info(f"[API /{operation.lower()}]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression'); reason = data.get('reason')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    if not reason: reason = "UNKNOWN_REASON"
    logger.info(f"[API /{operation.lower()}] Expr: {expression[:50]}... Reason: {reason}")
    try:
        failures_list = load_submission_failures(); found = False
        for item in failures_list:
            if isinstance(item, dict) and item.get('expression') == expression:
                item['reason'] = reason; item['timestamp'] = datetime.now(timezone.utc).isoformat(); found = True; break
        if not found:
            failures_list.append({ "expression": expression, "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat() })
        if save_submission_failures(failures_list): 
            try:
                logger.info(f"[API /{operation.lower()}] 正在执行二次检查... 从 'submitted_alphas.json' 中移除...")
                submitted_dict = load_submitted_alphas()
                if expression in submitted_dict:
                    submitted_dict.pop(expression, None) 
                    if not save_submitted_alphas(submitted_dict): logger.error(f"[API /{operation.lower()}] 二次检查：保存 submitted_alphas 失败。")
                    else: logger.info(f"[API /{operation.lower()}] 二次检查：成功从 submitted_alphas 中移除。")
            except Exception as e_secondary: logger.error(f"[API /{operation.lower()}] 二次检查时发生意外错误: {e_secondary}")
            return jsonify(status='success', message=f'已标记失败 (原因: {reason})')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='保存失败日志失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_failed_on_wq', methods=['POST'])
def unmark_alpha_failed():
    operation = "UnmarkFailed"; logger.info(f"[API /{operation.lower()}]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        failures_list = load_submission_failures(); original_size = len(failures_list)
        new_failures_list = [ item for item in failures_list if not (isinstance(item, dict) and item.get('expression') == expression) ]
        new_size = len(new_failures_list);
        if original_size == new_size: logger.warning(f"[API /{operation.lower()}] Expression not found.")
        if save_submission_failures(new_failures_list): return jsonify(status='success', message='已取消标记失败')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='保存失败日志失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/pending')
def pending_page():
    logger.info("[API /pending] Rendering pending alphas page.")
    return render_template('pending.html')

@app.route('/api/get_pending_alphas')
def get_pending_alphas():
    try:
        stats = get_hopeful_alphas_stats(); all_alphas = stats.get('all_alphas', [])
        pending_alphas = []
        for alpha in all_alphas:
            if (alpha.get('is_submittable') and not alpha.get('is_submitted') and not alpha.get('is_failed_on_wq')):
                alpha_perf = alpha.get('performance', {});
                if not isinstance(alpha_perf, dict): alpha_perf = {}
                pending_alphas.append({ "expression": alpha.get('expression'), "fitness": alpha_perf.get('fitness'), "sharpe": alpha_perf.get('sharpe'), "turnover": alpha_perf.get('turnover'), "returns": alpha_perf.get('returns'), "checks_summary": alpha.get('checks_summary'), "dashboard_score": alpha.get('dashboard_score'), "timestamp": alpha.get('timestamp') })
        pending_alphas.sort(key=lambda x: x.get('dashboard_score', -999.0) or -999.0, reverse=True)
        response = make_response(jsonify(pending_alphas))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.critical(f"[API /api/get_pending_alphas] CRITICAL Error: {e}", exc_info=True)
        return jsonify({"error": f"Failed to get pending alphas: {e}"}), 500

# v15.1: 添加日志查看器 (这部分我们之前讨论过，但未合并，现在加上)
LOG_DIR_FOR_VIEWER = "logs"
LINES_TO_READ = 500 

def get_last_n_lines(file_path, n):
    """高效读取文件最后 N 行"""
    if not os.path.exists(file_path):
        return f"错误：日志文件未找到。\n请检查路径: {file_path}\n"
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = deque(f, n)
            return "".join(lines)
    except Exception as e:
        return f"读取日志文件时发生错误: {e}\n"

@app.route('/view_log/<service_name>')
def view_log(service_name):
    """
    显示指定服务的日志 (最后 N 行)
    """
    # 假设 docker-compose.yml 中定义了 miner.log
    # 假设 docker-compose.evolver.yml 中定义了 evolver.log
    # 假设 docker-compose.dashboard.yml 中定义了 dashboard.log
    log_map = {
        "miner": "miner.log", # v13.3.7: 修复日志文件名 (从 miner.log 改为 miner_log.log 是错误的)
        "evolver": "evolver.log",
        "dashboard": "dashboard.log"
    }
    
    # v13.2.1 引入了 issues 日志
    log_map_issues = {
        "miner_issues": "miner_issues.log",
        "evolver_issues": "evolver_issues.log",
        "dashboard_issues": "dashboard_issues.log"
    }

    file_name = log_map.get(service_name) or log_map_issues.get(service_name)
    
    if not file_name:
        return "错误：未知的服务名称。", 404

    log_file_path = os.path.join(LOG_DIR_FOR_VIEWER, file_name)
    log_content = get_last_n_lines(log_file_path, LINES_TO_READ)
    
    response_html = f"""
    <html>
    <head>
        <title>{file_name}</title>
        <style>
            body {{ font-family: monospace; background-color: #2b2b2b; color: #f5f5f5; }}
            pre {{ white-space: pre-wrap; word-wrap: break-word; }}
        </style>
    </head>
    <body>
        <h3>显示日志: {file_name} (最后 {LINES_TO_READ} 行)</h3>
        <pre>{log_content}</pre>
    </body>
    </html>
    """
    return response_html
# --- v15.1: 结束 ---

if __name__ == '__main__':
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR); logger.info(f"Created log directory: {LOG_DIR}")
        except OSError as e: logger.error(f"Error creating log directory {LOG_DIR}: {e}")
    logger.info(f"Starting Flask application (Version: {CURRENT_DASHBOARD_VERSION})...")
    try:
        import pandas
        logger.info(f"Pandas version {pd.__version__} detected.")
    except ImportError:
         logger.warning("Pandas library not found. Timeseries API may not work.")

    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)