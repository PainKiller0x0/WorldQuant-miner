# --- Web仪表盘.py v11.0.4 (Stable Base v6.1.5 + Failure Reason) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone # v6.1.1: Added timezone
from collections import deque
import os.path
import logging
import pandas as pd # v10.0: Added for timeseries analysis
import numpy as np # v6.1.3: Import numpy for NaN checks

# --- v11.0.4: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v11.0.4"
# --- v11.0.4: 结束 ---

# --- 配置基础日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# v6.1.2: Configure static folder for Flask
app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
# v11.0.4: 文件路径调整
FAILED_SUBMISSIONS_FILE_OLD = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json')) # 旧文件路径
SUBMISSION_FAILURE_LOG_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submission_failure_log.json')) # 新日志文件路径
# v11.0.4: 结束
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
DASHBOARD_FILE_PATH = os.path.join(BASE_DIR, "Web仪表盘.py")
SYSTEM_CONFIG_FILE = os.path.join(BASE_DIR, 'system_config.json')
TESTED_ALPHAS_LOG_FILE = os.path.join(BASE_DIR, 'tested_alphas_log.json')

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
CACHE_DURATION = timedelta(seconds=300) # v6.1.1: Cache duration (5 minutes)

file_lock = threading.Lock() # submitted_alphas.json
hopeful_lock = threading.Lock() # hopeful_alphas.json
# v11.0.4: 修改锁名
failure_log_lock = threading.Lock() # 用于 submission_failure_log.json
# v11.0.4: 结束
config_lock = threading.Lock() # system_config.json
tested_log_lock = threading.Lock()

_timeseries_cache = None
_timeseries_cache_time = None
_cache_lock = threading.Lock()

# --- Load/Save functions (基于 v6.1.5) ---
def load_submitted_alphas():
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): return set() # 简化返回
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return set()
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, (list, set)):
                # logger.info(f"[Submit Load] Loaded {len(data)} items.") # 减少日志
                return set(data)
            else: logger.warning(f"[Submit Load] File {filepath} bad format."); return set()
        except Exception as e: logger.error(f"[Submit Load] Error loading {filepath}: {e}", exc_info=False); return set()

def save_submitted_alphas(submitted_set):
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        # logger.info(f"[Submit Save] Saving {len(submitted_set)} items.") # 减少日志
        try:
            if not isinstance(submitted_set, set):
                 logger.error(f"[Submit Save] Invalid data type: {type(submitted_set)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(submitted_set), f, indent=4)
            return True
        except Exception as e: logger.error(f"[Submit Save] Error saving {filepath}: {e}", exc_info=False); return False

# --- v11.0.4: 失败日志相关函数 ---
def load_submission_failures():
    """ (v11.0.4) 加载新的 submission_failure_log.json (返回字典列表)，含迁移逻辑 """
    with failure_log_lock:
        filepath = SUBMISSION_FAILURE_LOG_FILE
        if not os.path.exists(filepath):
            # logger.info(f"[Failure Log Load] File not found: {filepath}. Checking for old file.")
            old_set = load_failed_submissions_old_format() # 尝试加载旧格式

            if old_set:
                logger.warning(f"Found {len(old_set)} entries in old 'failed_submissions.json'. Migrating...")
                new_list = []
                for expr in old_set:
                    new_list.append({
                        "expression": expr,
                        "reason": "MIGRATED_UNKNOWN",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                # 内联保存逻辑以避免死锁
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(new_list, f, indent=4)
                    logger.info(f"Successfully migrated {len(new_list)} entries to new log.")
                    # 备份旧文件
                    try:
                        os.rename(FAILED_SUBMISSIONS_FILE_OLD, FAILED_SUBMISSIONS_FILE_OLD + ".bak")
                        logger.info(f"Old failure log backed up to {FAILED_SUBMISSIONS_FILE_OLD}.bak")
                    except Exception as e:
                        logger.error(f"Failed to rename old failure log: {e}")
                    return new_list # 返回迁移后的数据
                except Exception as e:
                    logger.error(f"Failed to save migrated failure log (inline): {e}. Returning empty list.")
                    return []
            return [] # 新旧文件都不存在

        # 如果新文件存在，则加载它
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: return []
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, list):
                # logger.info(f"[Failure Log Load] Loaded {len(data)} entries.")
                return data
            else: logger.warning(f"[Failure Log Load] File {filepath} bad format."); return []
        except Exception as e: logger.error(f"[Failure Log Load] Error loading {filepath}: {e}", exc_info=False); return []

def save_submission_failures(failures_list):
    """ (v11.0.4) 保存新的 submission_failure_log.json (写入字典列表) """
    with failure_log_lock:
        filepath = SUBMISSION_FAILURE_LOG_FILE
        # logger.info(f"[Failure Log Save] Saving {len(failures_list)} entries.")
        try:
            if not isinstance(failures_list, list):
                 logger.error(f"[Failure Log Save] Invalid data type: {type(failures_list)}."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(failures_list, f, indent=4)
            return True
        except Exception as e: logger.error(f"[Failure Log Save] Error saving {filepath}: {e}", exc_info=False); return False

def load_failed_submissions_old_format():
    """ (v11.0.4) 辅助函数，仅用于迁移旧数据 """
    filepath = FAILED_SUBMISSIONS_FILE_OLD
    if not os.path.exists(filepath): return set()
    try:
        # 简化文件检查
        if os.path.getsize(filepath) < 2: return set()
        with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
        if isinstance(data, (list, set)):
            return set(data)
    except Exception as e:
        logger.error(f"[Old Failed Load] Error loading {filepath}: {e}")
    return set()

def load_failed_submissions():
    """
    (v11.0.4) 重构以兼容 v6.1.5 get_hopeful_alphas_stats。
    读取新日志，返回失败表达式的 Set。
    """
    failures_list = load_submission_failures() # 读取新日志（安全）
    failed_expressions_set = set(item['expression'] for item in failures_list if isinstance(item, dict) and 'expression' in item)
    # logger.info(f"[Failed Set Load] Extracted {len(failed_expressions_set)} unique expressions.")
    return failed_expressions_set

# (v11.0.4) 不再需要旧的 save_failed_submissions(failed_set) 函数
# --- v11.0.4: 结束 ---

def get_service_status(log_file):
    status = "UNKNOWN"; last_seen = "Never"; logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            # 简化时间比较
            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT: status = "RUNNING"
            else: status = "STALLED"
            # 简化日志读取
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                latest_lines = deque(f, maxlen=50)
            logs = "".join(latest_lines) # deque 已经是末尾的行了
        except Exception as e: logs = f"Error reading log: {e}"; status = "ERROR"; logger.error(f"Error status for {log_file}: {e}", exc_info=False)
    else: status = "NOT FOUND"
    return {"status": status, "last_seen": last_seen, "logs": logs}


def get_hopeful_alphas_stats():
    # 保持 v6.1.5 结构，但使用新的 load_failed_submissions
    stats = { "count": 0, "max_fitness": -999.0, "max_sharpe": -999.0, "avg_fitness": 0.0, # 初始值设为更合理的值
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try: # v11.0.4: 增加整体异常捕获
        submitted_set = load_submitted_alphas()
        failed_set = load_failed_submissions() # 使用新函数
        # logger.info(f"[Stats] Using submitted ({len(submitted_set)}) and failed ({len(failed_set)}) sets.")

        pass_pattern = re.compile(r'(\d+)\s+PASS')
        fail_pattern = re.compile(r'(\d+)\s+FAIL')
        # pending_pattern = re.compile(r'(\d+)\s+PENDING') # 未在 v6.1.5 中使用
        alphas = []

        with hopeful_lock:
            if os.path.exists(HOPEFUL_ALPHAS_FILE):
                try:
                    # 简化文件检查和读取
                    if os.path.getsize(HOPEFUL_ALPHAS_FILE) > 1:
                         with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                             alphas_data = json.load(f)
                             if isinstance(alphas_data, list): alphas = alphas_data
                             else: logger.warning(f"[Stats] hopeful_alphas.json not a list.")
                except Exception as e: logger.error(f"[Stats] Error reading {HOPEFUL_ALPHAS_FILE}: {e}", exc_info=False)

        if alphas:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list)

            # --- v11.0.4: 更健壮的统计计算 (同 v11.0.2) ---
            valid_fitness = []
            valid_sharpe = []
            for a in valid_alphas_list:
                perf = a.get('performance')
                if isinstance(perf, dict):
                    try:
                        f_val = perf.get('fitness')
                        if f_val is not None: valid_fitness.append(float(f_val))
                    except (ValueError, TypeError): pass
                    try:
                        s_val = perf.get('sharpe')
                        if s_val is not None: valid_sharpe.append(float(s_val))
                    except (ValueError, TypeError): pass
            # --- v11.0.4: 结束 ---

            if valid_fitness:
                 # 使用 np.nanmax/np.nanmean 可能更安全，但这里保持简单
                 stats['max_fitness'] = max(valid_fitness) if valid_fitness else -999.0
                 stats['avg_fitness'] = sum(valid_fitness) / len(valid_fitness) if valid_fitness else 0.0
            if valid_sharpe:
                 stats['max_sharpe'] = max(valid_sharpe) if valid_sharpe else -999.0

            # 保持 v6.1.5 的评分逻辑
            def calculate_dashboard_score(report):
                if not isinstance(report, dict): return -float('inf')
                perf = report.get('performance', {})
                if not isinstance(perf, dict): return -float('inf')
                fitness = perf.get('fitness', -999); sharpe = perf.get('sharpe', 0.0); turnover = perf.get('turnover', 1.0)
                checks_summary = report.get('checks_summary', '0 PASS'); passed_count = 0
                try:
                    match = pass_pattern.search(checks_summary or '')
                    if match: passed_count = int(match.group(1))
                except (ValueError, TypeError): pass

                fitness_f = -999.0; sharpe_f = 0.0; turnover_f = 1.0
                try:
                   if fitness is not None: fitness_f = float(fitness)
                except (ValueError, TypeError): pass
                try:
                   if sharpe is not None: sharpe_f = float(sharpe)
                except (ValueError, TypeError): pass
                try:
                    if turnover is not None: turnover_f = float(turnover)
                except (ValueError, TypeError): pass

                return fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1)

            processed_alphas_temp = []
            successfully_submitted_count_local = 0
            total_submitted_count_local = 0

            for alpha_report in valid_alphas_list:
                try: # v11.0.4: 对每个 alpha 的处理也加上 try-except
                    expression = alpha_report.get('expression')
                    if not expression: continue
                    perf_data = alpha_report.get('performance', {});
                    if not isinstance(perf_data, dict): perf_data = {} # 确保是字典
                    summary_str = alpha_report.get('checks_summary', '') or ''
                    fail_match = fail_pattern.search(summary_str);
                    has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
                    pass_match = pass_pattern.search(summary_str);
                    passed_count = int(pass_match.group(1)) if pass_match else 0
                    is_submittable = passed_count >= 7 and not has_fail
                    is_submitted = expression in submitted_set
                    is_failed_on_wq = expression in failed_set # 使用新的 failed_set
                    is_successfully_submitted = is_submittable and is_submitted and not is_failed_on_wq

                    if is_submittable and not is_submitted and not is_failed_on_wq: stats['submittable_pending_count'] += 1

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
        # 返回默认空 stats，避免 /status 接口完全失败
        stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
                  "submittable_pending_count": 0, "successfully_submitted_count": 0,
                  "total_submitted_count": 0, "all_alphas": [] }
    return stats


def get_version_from_file(file_path, version_regex_str):
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
    # v11.0.4: 指向修改后的 legacy 模板
    return render_template('dashboard_v4_legacy.html', settings_page=True, chart_page=True)

@app.route('/settings')
def settings_page():
    return render_template('settings.html') # 保持不变

@app.route('/chart')
def chart_page():
    return render_template('chart.html') # 保持不变

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    # logger.info("[API /api/get_settings]")
    with config_lock:
        try:
            if not os.path.exists(SYSTEM_CONFIG_FILE):
                return jsonify({"error": "Config file not found."}), 404
            with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
            response = make_response(jsonify(data))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response
        except Exception as e:
            logger.error(f"[API /api/get_settings] Error: {e}", exc_info=False)
            return jsonify({"error": "Error reading config."}), 500

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    # 保持 v6.1.5 的实现，只修改日志级别
    logger.info("[API /api/save_settings]")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    new_config = request.json
    expected_keys = { "wq_api_cooldown": int, "llm_api_cooldown": int, "miner_concurrency": int,
                      "miner_sleep": int, "evolver_concurrency": int, "evolver_sleep": int,
                      "producer_queue_full_sleep": int }
    if not isinstance(new_config, dict): return jsonify(status='error', message='Invalid JSON format'), 400
    validated_config = {}
    with config_lock:
        try:
            # 读取现有配置
            if os.path.exists(SYSTEM_CONFIG_FILE):
                try:
                    with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: validated_config = json.load(f)
                except json.JSONDecodeError: validated_config = {} # 如果损坏则覆盖

            # 验证并更新键值
            for key, expected_type in expected_keys.items():
                if key in new_config:
                    value = new_config[key]
                    try:
                        converted_value = expected_type(value)
                        if converted_value < 0: return jsonify(status='error', message=f"{key} must be >= 0"), 400
                        validated_config[key] = converted_value
                    except (ValueError, TypeError):
                         return jsonify(status='error', message=f"Invalid type for {key}."), 400
                elif key not in validated_config: # 确保所有预期的键都存在
                     return jsonify(status='error', message=f"Missing key: {key}"), 400

            # 更新搜索空间 (如果提供)
            validated_config['evolver_search_space'] = new_config.get('evolver_search_space', validated_config.get('evolver_search_space', {}))

            # 保存配置
            with open(SYSTEM_CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(validated_config, f, indent=2)
            logger.info(f"[API /api/save_settings] Config saved.")
            # 使时间序列缓存失效
            global _timeseries_cache, _timeseries_cache_time
            with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
            return jsonify(status='success', message='配置已保存') # 中文提示
        except Exception as e:
            logger.error(f"[API /api/save_settings] Error: {e}", exc_info=True)
            return jsonify(status='error', message="保存配置时发生内部错误。"), 500


@app.route('/status')
def status():
    # logger.info("[API /status] Request received.") # 减少日志频率
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
        # 提供更详细的错误信息
        error_data = {
             "miner": {"status": "ERROR", "logs": f"Failed: {e}"},
             "evolver": {"status": "ERROR", "logs": f"Failed: {e}"},
             "hopeful_alphas": {"count": 0, "all_alphas": [], "error": f"Failed: {e}"} }
        return jsonify(error_data), 500

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
                return jsonify({"error": "Internal server error."}), 500


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'miner_issues.log', 'evolver_issues.log'] # 保持 v6.1.5 一致
    if log_filename not in allowed_files: return "Invalid log file requested", 404
    try: return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except Exception as e: logger.error(f"[API /download_logs] Error: {e}"); return "Error downloading file", 500

# --- v11.0.4: Mark/Unmark API 使用新日志 ---
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
        failures_list = load_submission_failures() # 读取新日志
        found = False
        for item in failures_list:
            if isinstance(item, dict) and item.get('expression') == expression:
                item['reason'] = reason; item['timestamp'] = datetime.now(timezone.utc).isoformat()
                found = True; break
        if not found:
            failures_list.append({ "expression": expression, "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat() })
        if save_submission_failures(failures_list): # 保存新日志
            return jsonify(status='success', message=f'已标记失败 (原因: {reason})')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='保存失败日志失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500

@app.route('/api/unmark_failed_on_wq', methods=['POST'])
def unmark_alpha_failed():
    operation = "UnmarkFailed"; logger.info(f"[API /{operation.lower()}]")
    if not request.is_json: return jsonify(status='error', message='请求必须是 JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='无效的表达式'), 400
    try:
        failures_list = load_submission_failures() # 读取新日志
        original_size = len(failures_list)
        new_failures_list = [ item for item in failures_list if not (isinstance(item, dict) and item.get('expression') == expression) ]
        new_size = len(new_failures_list)
        if original_size == new_size: logger.warning(f"[API /{operation.lower()}] Expression not found.")
        if save_submission_failures(new_failures_list): # 保存新日志
            return jsonify(status='success', message='已取消标记失败')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='保存失败日志失败'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] Error: {e}", exc_info=True); return jsonify(status='error', message='服务器内部错误'), 500
# --- v11.0.4: 结束 ---


if __name__ == '__main__':
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR); logger.info(f"Created log directory: {LOG_DIR}")
        except OSError as e: logger.error(f"Error creating log directory {LOG_DIR}: {e}")
    # 移除 os.stat_cache() 调用
    logger.info(f"Starting Flask application (Version: {CURRENT_DASHBOARD_VERSION})...")
    try:
        import pandas
        logger.info(f"Pandas version {pd.__version__} detected.")
    except ImportError:
         logger.warning("Pandas library not found. Timeseries API may not work.")

    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)