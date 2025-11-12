# --- Web仪表盘.py v14.1 (适配 v14.0 双轨制预算) ---
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

import utils

# --- v14.1: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v14.1 (Dual Budget & Doubao Support)"
# --- v14.1: 结束 ---

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')

SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
SUBMISSION_FAILURE_LOG_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submission_failure_log.json')) 
FAILED_SUBMISSIONS_FILE_OLD = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json')) 
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
TESTED_ALPHAS_LOG_FILE = os.path.join(BASE_DIR, 'tested_alphas_log.json')

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
CACHE_DURATION = timedelta(seconds=60) 

file_lock = threading.Lock()
failure_log_lock = threading.Lock() 
tested_log_lock = threading.Lock()

_timeseries_cache = None
_timeseries_cache_time = None
_cache_lock = threading.Lock()

_submission_cache = None
_submission_cache_time = None
_submission_cache_lock = threading.Lock()

def load_submitted_alphas():
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): return {}
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return {}
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            final_dict = {}; needs_resave = False; migration_timestamp = None
            if isinstance(data, (list, set)):
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
                    needs_resave = True
                    for item in final_dict.values():
                        if isinstance(item, dict) and "reason" not in item:
                            if item.get('manual_timestamp') == zombie_timestamp: item["reason"] = "MIGRATED_UNKNOWN"
                            else: item["reason"] = "MANUAL_ADD"
            else:
                return {}
            if needs_resave:
                try:
                    with open(filepath, 'w', encoding='utf-8') as f_save: json.dump(final_dict, f_save, indent=4)
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

def load_failed_submissions_old_format():
    filepath = FAILED_SUBMISSIONS_FILE_OLD
    if not (os.path.exists(filepath) and os.path.isfile(filepath)): return set()
    try:
        if os.path.getsize(filepath) < 2: return set()
        with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
        if isinstance(data, (list, set)): return set(data)
        else: return set()
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
            old_set = load_failed_submissions_old_format()
            if old_set:
                current_expressions = {item['expression'] for item in current_failures_list if isinstance(item, dict)}
                items_to_migrate = []
                for expr in old_set:
                    if expr not in current_expressions:
                        items_to_migrate.append({ "expression": expr, "reason": "MIGRATED_UNKNOWN", "timestamp": datetime.now(timezone.utc).isoformat() })
                if items_to_migrate:
                    current_failures_list.extend(items_to_migrate); needs_resave = True
                try: os.rename(old_filepath, old_filepath + ".bak");
                except Exception as rename_e: logger.error(f"Failed to rename old failure log: {rename_e}")
            else:
                try: os.rename(old_filepath, old_filepath + ".bak")
                except Exception as rename_e: logger.error(f"Failed to rename old failure log: {rename_e}")
        elif not new_file_exists:
             try:
                with open(new_filepath, 'w', encoding='utf-8') as f: json.dump([], f)
             except Exception as create_e: logger.error(f"Failed to create new empty log file! {create_e}", exc_info=True)
        if needs_resave:
            try:
                with open(new_filepath, 'w', encoding='utf-8') as f: json.dump(current_failures_list, f, indent=4)
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

def get_hopeful_alphas_stats():
    stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try: 
        submitted_dict = load_submitted_alphas() 
        failed_map, failed_set = load_failed_submissions() 
        alphas = utils.load_hopeful_alphas_safe() 
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
                  "total_submitted_count": 0, "all_alphas": [], "error": f"获取统计时出错: {e}" }
    return stats

def get_version_from_file(file_path, version_regex_str):
    version_regex = re.compile(version_regex_str)
    try:
        if not os.path.isfile(file_path): return "file_not_found"
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        match = version_regex.search(content)
        return match.group(1) if match else "unknown_format"
    except Exception as e: logger.error(f"[Version] Error reading {file_path}: {e}"); return "read_error"

# --- v14.1: 修正首页渲染 ---
@app.route('/')
def dashboard():
    # 强制使用新的 dashboard_v4.html，不再使用 legacy
    return render_template('dashboard_v4.html', settings_page=True, chart_page=True)
# --- v14.1 结束 ---

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/chart')
def chart_page():
    return render_template('chart.html')

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    try:
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
    logger.info("[API /api/save_settings] (v14.2 Dual Budget Support)")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    new_data = request.json
    if not isinstance(new_data, dict): return jsonify(status='error', message='无效的 JSON 格式'), 400

    try:
        current_config = utils.load_system_config()
        
        # 1. 处理顶级静态参数
        static_keys = ['miner_concurrency', 'evolver_concurrency', 'producer_queue_full_sleep', 'hopeful_pool_max_size', 'evolver_wildcard_count']
        for key in static_keys:
            if key in new_data:
                try: current_config[key] = int(new_data[key])
                except (ValueError, TypeError): return jsonify(status='error', message=f"无效的 {key} (必须是整数)"), 400
        
        # 2. 处理双轨制预算 (llm_budgets)
        # 注意：这里只更新 limit，必须保留 used_today 和 dates
        if 'llm_budgets' in new_data and isinstance(new_data['llm_budgets'], dict):
            if 'llm_budgets' not in current_config: 
                current_config['llm_budgets'] = {}
            
            for role in ['miner', 'evolver']:
                if role in new_data['llm_budgets']:
                    input_role_data = new_data['llm_budgets'][role]
                    if 'daily_limit' in input_role_data:
                        # 确保目标字典存在
                        if role not in current_config['llm_budgets']:
                            current_config['llm_budgets'][role] = {
                                "daily_limit": 0, "used_today": 0, "last_used_date_utc": "1970-01-01"
                            }
                        # 仅更新限额
                        try:
                            current_config['llm_budgets'][role]['daily_limit'] = int(input_role_data['daily_limit'])
                        except (ValueError, TypeError):
                            return jsonify(status='error', message=f"无效的 {role} budget (必须是整数)"), 400

        # 3. 处理 WQ 速率限制
        wq_keys = ['wq_429_cooldown_seconds', 'max_tpm_limit', 'min_tpm_limit']
        if 'wq_api_limiter' in new_data and isinstance(new_data['wq_api_limiter'], dict):
            if 'wq_api_limiter' not in current_config: current_config['wq_api_limiter'] = {}
            for key in wq_keys:
                 if key in new_data['wq_api_limiter']:
                    try: current_config['wq_api_limiter'][key] = int(new_data['wq_api_limiter'][key])
                    except (ValueError, TypeError): return jsonify(status='error', message=f"无效的 {key} (必须是整数)"), 400

        if 'evolver_search_space' in new_data:
            current_config['evolver_search_space'] = new_data.get('evolver_search_space', current_config.get('evolver_search_space', {}))

        if utils.save_system_config(current_config):
            logger.info(f"[API /api/save_settings] 配置已成功保存。")
            global _timeseries_cache, _timeseries_cache_time
            with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
            return jsonify(status='success', message='配置已保存')
        else:
            logger.error(f"[API /api/save_settings] 保存失败。")
            return jsonify(status='error', message='保存配置时发生内部错误。'), 500
    except Exception as e:
        logger.error(f"[API /api/save_settings] Error: {e}", exc_info=True)
        return jsonify(status='error', message="保存配置时发生内部错误。"), 500

# --- v14.1: 适配双轨制预算的 Status 接口 ---
@app.route('/status')
def status():
    try:
        data = { 
            "miner": get_service_status('miner.log'),
            "evolver": get_service_status('evolver.log'),
            "hopeful_alphas": get_hopeful_alphas_stats() 
        } 
                 
        try:
            config = utils.load_system_config()
            
            # v14.1: 读取新的 llm_budgets 结构
            llm_budgets = config.get("llm_budgets", {})
            miner_budget = llm_budgets.get("miner", {})
            evolver_budget = llm_budgets.get("evolver", {})
            
            # 兼容旧版
            old_budget_style = miner_budget
            
            wq_limiter = config.get("wq_api_limiter", {})
            
            wq_cooldown_status = "OK"
            wq_cooldown_remaining = 0
            last_failure = wq_limiter.get("last_failure_timestamp", 0)
            cooldown_period = wq_limiter.get("wq_429_cooldown_seconds", 60)
            now = time.time()
            
            if now - last_failure < cooldown_period:
                wq_cooldown_remaining = round(cooldown_period - (now - last_failure))
                wq_cooldown_status = f"IN_COOLDOWN ({wq_cooldown_remaining}s)"

            # v14.1: 返回更丰富的数据结构
            data["watchdog_status"] = {
                # 兼容字段
                "llm_budget_used": old_budget_style.get("used_today", 0), # <--- 修正
                "llm_budget_limit": old_budget_style.get("daily_limit", 2000),
                "llm_budget_date_utc": old_budget_style.get("last_used_date_utc", "N/A"),
                
                # v14.1 新字段
                "miner_budget_used": miner_budget.get("used_today", 0), # <--- 修正
                "miner_budget_limit": miner_budget.get("daily_limit", 0),
                "evolver_budget_used": evolver_budget.get("used_today", 0), # <--- 修正
                "evolver_budget_limit": evolver_budget.get("daily_limit", 0),
                
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
# --- v14.1 结束 ---

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
            logger.debug("返回缓存的 /api/v1/stats/timeseries")
            return jsonify(_timeseries_cache)
        
        with tested_log_lock:
            if not os.path.exists(TESTED_ALPHAS_LOG_FILE): 
                return jsonify({"error": "Log file not found."}), 404
            
            try:
                if os.path.getsize(TESTED_ALPHAS_LOG_FILE) < 2:
                    logger.warning(f"[API Timeseries] {TESTED_ALPHAS_LOG_FILE} 为空。")
                    _timeseries_cache = {"timestamps": [], "count": [], "mean_fitness": [], "high_quality_count": []}
                    _timeseries_cache_time = now; return jsonify(_timeseries_cache)

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
                
                if not output_df.empty:
                    output_df = output_df[ (output_df['count'] > 0) | (output_df['high_quality_count'] > 0) ]

                if output_df.empty:
                    _timeseries_cache = {"timestamps": [], "count": [], "mean_fitness": [], "high_quality_count": []}
                    _timeseries_cache_time = now; return jsonify(_timeseries_cache)

                output = { 
                    "timestamps": output_df.index.strftime('%m-%d %H:00').tolist(), 
                    "count": output_df['count'].tolist(), 
                    "mean_fitness": output_df['mean_fitness'].round(4).replace({np.nan: None}).tolist(), 
                    "high_quality_count": output_df['high_quality_count'].tolist() 
                }
                
                _timeseries_cache = output; _timeseries_cache_time = now
                logger.info(f"重新生成 /api/v1/stats/timeseries 缓存 (过滤后 {len(output['timestamps'])} 条)")
                return jsonify(output)
            except Exception as e:
                logger.error(f"[API Timeseries] Error: {e}", exc_info=True)
                with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                return jsonify({"error": "内部服务器错误。"}), 500
def get_daily_submission_stats():
    stats = { "timestamps": [], "submittable_count": [], "submitted_count": [], "failed_count": [] }
    try:
        # 1. 统计 Submitted (按 manual_timestamp 的日期)
        submitted_data = load_submitted_alphas()
        submitted_counter = Counter()
        for item in submitted_data.values():
            ts = item.get('manual_timestamp')
            if ts and len(ts) >= 10:
                date_str = ts[:10] # YYYY-MM-DD
                submitted_counter[date_str] += 1
        
        # 2. 统计 Failed (按 timestamp 的日期)
        failed_list = load_submission_failures()
        failed_counter = Counter()
        for item in failed_list:
            if isinstance(item, dict):
                ts = item.get('timestamp')
                if ts and len(ts) >= 10:
                    date_str = ts[:10]
                    failed_counter[date_str] += 1
        
        # 3. 统计 Submittable (来自 Hopeful Pool，按生成日期)
        # 注意：这里只统计目前还在池子里的。
        alphas = utils.load_hopeful_alphas_safe()
        submittable_counter = Counter()
        
        pass_pattern = re.compile(r'(\d+)\s+PASS')
        fail_pattern = re.compile(r'(\d+)\s+FAIL')
        
        for alpha in alphas:
            if not isinstance(alpha, dict): continue
            
            # 检查是否 Submittable
            summary_str = alpha.get('checks_summary', '') or ''
            
            # 解析 Checks
            fail_match = fail_pattern.search(summary_str)
            has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
            pass_match = pass_pattern.search(summary_str)
            passed_count = int(pass_match.group(1)) if pass_match else 0
            
            is_submittable = (passed_count >= 7 and not has_fail)
            
            if is_submittable:
                ts = alpha.get('timestamp') # e.g., "2023-01-01 12:00:00"
                if ts and len(ts) >= 10:
                    date_str = ts[:10]
                    submittable_counter[date_str] += 1

        # 4. 合并日期并排序
        all_dates = set(submitted_counter.keys()) | set(failed_counter.keys()) | set(submittable_counter.keys())
        sorted_dates = sorted([d for d in all_dates if re.match(r'^\d{4}-\d{2}-\d{2}$', d)])
        
        # 5. 组装数据
        stats['timestamps'] = sorted_dates
        stats['submitted_count'] = [submitted_counter[d] for d in sorted_dates]
        stats['failed_count'] = [failed_counter[d] for d in sorted_dates]
        stats['submittable_count'] = [submittable_counter[d] for d in sorted_dates]
        
    except Exception as e:
        logger.error(f"[Daily Stats] Error: {e}", exc_info=True)
    
    return stats
@app.route('/api/v1/stats/submission_daily')
def api_stats_submission_daily():
    global _submission_cache, _submission_cache_time
    with _submission_cache_lock:
        now = datetime.now(timezone.utc)
        
        if _submission_cache and _submission_cache_time and (now - _submission_cache_time < CACHE_DURATION):
            logger.debug("返回缓存的 /api/v1/stats/submission_daily")
            return jsonify(_submission_cache)
        try:
            logger.info("重新生成 /api/v1/stats/submission_daily 缓存")
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

LOG_DIR_FOR_VIEWER = "logs"
LINES_TO_READ = 500 

def get_last_n_lines(file_path, n):
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
    log_map = {
        "miner": "miner.log", 
        "evolver": "evolver.log",
        "dashboard": "dashboard.log"
    }
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