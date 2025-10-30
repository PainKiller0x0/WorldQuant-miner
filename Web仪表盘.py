# --- Web仪表盘.py v11.0.12 (修复“干掉 Hopeful Pool”的 Bug) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
import os.path
import logging
import pandas as pd
import numpy as np

# --- v11.0.12: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v11.0.12"
# --- v11.0.12: 结束 ---

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

# --- Load/Save (基本保持 v6.1.5，增加失败日志处理) ---
def load_submitted_alphas():
    # 保持 v6.1.5 逻辑
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): return set()
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return set()
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, (list, set)):
                return set(data)
            else: logger.warning(f"[Submit Load] File {filepath} bad format."); return set()
        except Exception as e: logger.error(f"[Submit Load] Error loading {filepath}: {e}", exc_info=False); return set()

def save_submitted_alphas(submitted_set):
    # 保持 v6.1.5 逻辑
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        try:
            if not isinstance(submitted_set, set):
                 logger.error(f"[Submit Save] Invalid data type: {type(submitted_set)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(submitted_set), f, indent=4)
            return True
        except Exception as e: logger.error(f"[Submit Save] Error saving {filepath}: {e}", exc_info=False); return False

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
    """ (v11.0.10) 重写加载逻辑，修复“僵尸”Alpha (迁移) Bug """
    with failure_log_lock:
        new_filepath = SUBMISSION_FAILURE_LOG_FILE
        old_filepath = FAILED_SUBMISSIONS_FILE_OLD
        
        current_failures_list = []
        new_file_exists = os.path.exists(new_filepath)
        new_file_has_content = new_file_exists and os.path.isfile(new_filepath) and os.path.getsize(new_filepath) > 2
        old_file_exists_and_not_backed_up = os.path.exists(old_filepath) and not os.path.exists(old_filepath + ".bak")

        # --- Path 1: 尝试加载新日志文件 (你手动的5条) ---
        if new_file_has_content:
            try:
                with open(new_filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    current_failures_list = data
                    # logger.info(f"[Failure Log Load] Loaded {len(current_failures_list)} entries from {new_filepath}")
                else:
                    logger.error(f"[Failure Log Load] {new_filepath} is not a list. Re-initializing.")
            except Exception as e:
                logger.error(f"[Failure Log Load] Error loading {new_filepath}: {e}. Attempting recovery.")

        # --- Path 2: 检查是否需要从旧日志迁移 (你的30+条) ---
        if old_file_exists_and_not_backed_up:
            logger.warning(f"[Failure Log Load] Old log '{old_filepath}' still exists. Checking for merge/migration...")
            old_set = load_failed_submissions_old_format()
            
            if old_set:
                # 找出新日志中已有的表达式
                current_expressions = {item['expression'] for item in current_failures_list if isinstance(item, dict)}
                
                # 找出旧日志中需要合并的新条目
                items_to_migrate = []
                for expr in old_set:
                    if expr not in current_expressions:
                        items_to_migrate.append({"expression": expr, "reason": "MIGRATED_UNKNOWN", "timestamp": datetime.now(timezone.utc).isoformat()})
                
                if items_to_migrate:
                    logger.warning(f"Found {len(items_to_migrate)} new entries in old log. Merging...")
                    current_failures_list.extend(items_to_migrate)
                    
                    # 立即保存合并后的列表
                    try:
                        with open(new_filepath, 'w', encoding='utf-8') as f:
                            json.dump(current_failures_list, f, indent=4)
                        logger.info(f"Successfully merged and saved {len(current_failures_list)} total entries to {new_filepath}.")
                    except Exception as save_e:
                        logger.error(f"Failed to save merged failure log (inline): {save_e}.")
                
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
    (v11.0.5) 兼容 v6.1.5 get_hopeful_alphas_stats。
    读取新日志，返回失败表达式的 Set。
    """
    # 这个调用现在更安全，会处理文件不存在/错误的情况
    failures_list = load_submission_failures()
    failed_expressions_set = set(item['expression'] for item in failures_list if isinstance(item, dict) and 'expression' in item)
    # logger.info(f"[Failed Set Load] Extracted {len(failed_expressions_set)} unique expressions.")
    return failed_expressions_set
# --- v11.0.10: 结束 ---

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
    # 保持 v6.1.5 逻辑，仅修改了 load_failed_submissions 调用点
    stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try: # v11.0.5: 保持整体异常捕获
        submitted_set = load_submitted_alphas()
        failed_set = load_failed_submissions() # <-- v11.0.10: 使用新的、健壮的实现
        
        # --- v11.0.12: 恢复 v11.0.5 的加载逻辑 (修复“干掉 hopeful 池”Bug) ---
        alphas = []
        with hopeful_lock:
            if os.path.exists(HOPEFUL_ALPHAS_FILE):
                try:
                    if os.path.getsize(HOPEFUL_ALPHAS_FILE) > 1:
                         with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                             alphas_data = json.load(f)
                             if isinstance(alphas_data, list): alphas = alphas_data
                             else: logger.warning(f"[Stats] hopeful_alphas.json not a list.")
                except Exception as e: logger.error(f"[Stats] Error reading {HOPEFUL_ALPHAS_FILE}: {e}", exc_info=False)
        # --- v11.0.12: 结束 ---

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
                # ... (v6.1.5 的 calculate_dashboard_score 函数体保持不变) ...
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

            pass_pattern = re.compile(r'(\d+)\s+PASS') # v11.0.11: 移到循环外
            fail_pattern = re.compile(r'(\d+)\s+FAIL') # v11.0.11: 移到循环外

            for alpha_report in valid_alphas_list:
                try: # 保持对每个 alpha 的处理加 try-except
                    expression = alpha_report.get('expression')
                    if not expression: continue
                    
                    # v11.0.12: 恢复 v11.0.10 的逻辑。我们 *必须* 处理所有 Alpha，
                    # UI/API (如 /api/get_pending_alphas) 会负责过滤
                    is_failed_on_wq = expression in failed_set 
                    is_submitted = expression in submitted_set

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

                    if is_submitted:
                         total_submitted_count_local += 1
                         if is_successfully_submitted:
                             successfully_submitted_count_local += 1

                    processed_alpha_data = {
                        "expression": expression, "timestamp": alpha_report.get('timestamp', 'N/A'),
                        "checks_summary": summary_str, "is_submittable": is_submittable,
                        "is_submitted": is_submitted, "is_failed_on_wq": is_failed_on_wq,
                        "is_successfully_submitted": is_successfully_submitted,
                        "dashboard_score": calculate_dashboard_score(alpha_report), # 保持 v6.1.5 评分
                        "performance": perf_data
                    }
                    processed_alphas_temp.append(processed_alpha_data)
                except Exception as e: logger.error(f"[Stats Process Alpha] Error for {expression[:30]}...: {e}", exc_info=False)

            stats['all_alphas'] = processed_alphas_temp
            stats['successfully_submitted_count'] = successfully_submitted_count_local
            stats['total_submitted_count'] = total_submitted_count_local
            # logger.info(f"[Stats] Finished processing {len(processed_alphas_temp)} alphas.")

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


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    # 保持 v6.1.5 允许的文件列表
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files: return "无效的日志文件请求", 404
    try: return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except Exception as e: logger.error(f"[API /download_logs] Error: {e}"); return "下载文件时出错", 500

# --- v11.0.5: 更新 Mark/Unmark APIs ---
@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    operation = "Mark"; # logger.info(f"[API /{operation.lower()}_submitted]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_set = load_submitted_alphas(); submitted_set.add(expression);
        if save_submitted_alphas(submitted_set): return jsonify(status='success', message='标记成功')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    operation = "Unmark"; # logger.info(f"[API /{operation.lower()}_submitted]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        submitted_set = load_submitted_alphas(); submitted_set.discard(expression);
        if save_submitted_alphas(submitted_set): return jsonify(status='success', message='取消标记成功')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='保存状态失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

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
        failures_list = load_submission_failures() # v11.0.10: 使用新的、健壮的实现
        found = False
        for item in failures_list:
            if isinstance(item, dict) and item.get('expression') == expression:
                item['reason'] = reason; item['timestamp'] = datetime.now(timezone.utc).isoformat()
                found = True; break
        
        if not found:
            failures_list.append({ "expression": expression, "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat() })
        
        # 无论 'found' 是 True 还是 False，这个保存操作都必须执行
        if save_submission_failures(failures_list): # 保存新日志
            
            # --- BEGIN v11.0.7 二次检查 (自动取消提交) ---
            try:
                logger.info(f"[API /{operation.lower()}] 正在执行二次检查... 从 'submitted_alphas.json' 中移除...")
                submitted_set = load_submitted_alphas()
                if expression in submitted_set:
                    submitted_set.discard(expression)
                    if not save_submitted_alphas(submitted_set):
                         logger.error(f"[API /{operation.lower()}] 二次检查：保存 submitted_alphas 失败。")
                    else:
                         logger.info(f"[API /{operation.lower()}] 二次检查：成功从 submitted_alphas 中移除。")
            except Exception as e_secondary:
                logger.error(f"[API /{operation.lower()}] 二次检查时发生意外错误: {e_secondary}")
            # --- END v11.0.7 ---

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
# --- v11.0.5: 结束 ---
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
        
        # v11.0.12: 这里的过滤逻辑现在依赖于 get_hopeful_alphas_stats() 
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