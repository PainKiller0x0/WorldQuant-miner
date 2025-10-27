# --- Web仪表盘.py v6.1.5 (Add Submission Stats & Fix Sticky Header Logic) ---
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

# --- v6.1.5: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v6.1.5"
# --- v6.1.5: 结束 ---

# --- 配置基础日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# v6.1.2: Configure static folder for Flask
app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
FAILED_SUBMISSIONS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json'))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
DASHBOARD_FILE_PATH = os.path.join(BASE_DIR, "Web仪表盘.py")
SYSTEM_CONFIG_FILE = os.path.join(BASE_DIR, 'system_config.json')
TESTED_ALPHAS_LOG_FILE = os.path.join(BASE_DIR, 'tested_alphas_log.json')


HEARTBEAT_TIMEOUT = timedelta(minutes=10)
CACHE_DURATION = timedelta(seconds=300) # v6.1.1: Cache duration (5 minutes)

file_lock = threading.Lock() # submitted_alphas.json
hopeful_lock = threading.Lock() # hopeful_alphas.json
failed_lock = threading.Lock() # failed_submissions.json
config_lock = threading.Lock() # system_config.json
tested_log_lock = threading.Lock()

# v6.1.1: Global cache variables
_timeseries_cache = None
_timeseries_cache_time = None
_cache_lock = threading.Lock() # Lock for accessing cache variables

# --- Load/Save functions (load_submitted_alphas, save_submitted_alphas, load_failed_submissions, save_failed_submissions) remain unchanged ---
def load_submitted_alphas():
    # ... (代码不变) ...
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): logger.info(f"[Submit Load] File not found: {filepath}. Returning empty set."); return set()
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: logger.info(f"[Submit Load] File empty/invalid: {filepath}. Returning empty set."); return set()
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, (list, set)):
                loaded_set = set(data); sample = list(loaded_set)[:3]
                logger.info(f"[Submit Load] Successfully loaded {len(loaded_set)} items from {filepath}. Sample: {sample}"); return loaded_set
            else: logger.warning(f"[Submit Load] File {filepath} did not contain a list/set. Found type: {type(data)}. Returning empty set."); return set()
        except json.JSONDecodeError as e: logger.error(f"[Submit Load] Error decoding JSON from {filepath}: {e}. Returning empty set."); return set()
        except IOError as e: logger.error(f"[Submit Load] IOError reading {filepath}: {e}. Returning empty set."); return set()
        except Exception as e: logger.error(f"[Submit Load] Unexpected error loading {filepath}: {e}", exc_info=True); return set()

def save_submitted_alphas(submitted_set):
    # ... (代码不变) ...
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        logger.info(f"[Submit Save] Attempting to save {len(submitted_set)} items to {filepath}")
        try:
            if not isinstance(submitted_set, set):
                 logger.error(f"[Submit Save] Invalid data type passed: {type(submitted_set)}. Aborting save."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(submitted_set), f, indent=4)
            logger.info(f"[Submit Save] Successfully saved {len(submitted_set)} items to {filepath}"); return True
        except IOError as e: logger.error(f"[Submit Save] IOError saving {filepath}: {e}"); return False
        except Exception as e: logger.error(f"[Submit Save] Unexpected error saving {filepath}: {e}", exc_info=True); return False

def load_failed_submissions():
    # ... (代码不变) ...
    with failed_lock:
        filepath = FAILED_SUBMISSIONS_FILE
        if not os.path.exists(filepath): logger.info(f"[Failed Load] File not found: {filepath}. Returning empty set."); return set()
        try:
            if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: logger.info(f"[Failed Load] File empty/invalid: {filepath}. Returning empty set."); return set()
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, (list, set)):
                loaded_set = set(data); sample = list(loaded_set)[:3]
                logger.info(f"[Failed Load] Successfully loaded {len(loaded_set)} failed items from {filepath}. Sample: {sample}"); return loaded_set
            else: logger.warning(f"[Failed Load] File {filepath} did not contain a list/set. Found type: {type(data)}. Returning empty set."); return set()
        except json.JSONDecodeError as e: logger.error(f"[Failed Load] Error decoding JSON from {filepath}: {e}. Returning empty set."); return set()
        except IOError as e: logger.error(f"[Failed Load] IOError reading {filepath}: {e}. Returning empty set."); return set()
        except Exception as e: logger.error(f"[Failed Load] Unexpected error loading {filepath}: {e}", exc_info=True); return set()

def save_failed_submissions(failed_set):
    # ... (代码不变) ...
    with failed_lock:
        filepath = FAILED_SUBMISSIONS_FILE
        logger.info(f"[Failed Save] Attempting to save {len(failed_set)} items to {filepath}")
        try:
            if not isinstance(failed_set, set):
                 logger.error(f"[Failed Save] Invalid data type passed: {type(failed_set)}. Aborting save."); return False
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(failed_set), f, indent=4)
            logger.info(f"[Failed Save] Successfully saved {len(failed_set)} items to {filepath}"); return True
        except IOError as e: logger.error(f"[Failed Save] IOError saving {filepath}: {e}"); return False
        except Exception as e: logger.error(f"[Failed Save] Unexpected error saving {filepath}: {e}", exc_info=True); return False

# --- get_service_status remains unchanged ---
def get_service_status(log_file):
    # ... (代码不变) ...
    status = "UNKNOWN"; last_seen = "Never"; logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT: status = "RUNNING"
            else: status = "STALLED"
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: latest_lines_deque = deque(f, maxlen=50)
            latest_lines = list(latest_lines_deque); latest_lines.reverse(); logs = "".join(latest_lines)
        except Exception as e: logs = f"Error reading log file: {e}"; status = "ERROR"; logger.error(f"Error getting service status for {log_file}: {e}", exc_info=True)
    else: status = "NOT FOUND"
    return {"status": status, "last_seen": last_seen, "logs": logs}


# --- v6.1.5: Add submission counts to get_hopeful_alphas_stats ---
def get_hopeful_alphas_stats():
    stats = {
        "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
        "submittable_pending_count": 0,
        "successfully_submitted_count": 0, # v6.1.5 New
        "total_submitted_count": 0,       # v6.1.5 New
        "all_alphas": []
    }
    submitted_set = load_submitted_alphas()
    failed_set = load_failed_submissions()
    logger.info(f"[Stats] Loaded submitted set ({len(submitted_set)} items) and failed set ({len(failed_set)} items) for stats calculation.")

    pass_pattern = re.compile(r'(\d+)\s+PASS')
    fail_pattern = re.compile(r'(\d+)\s+FAIL')
    pending_pattern = re.compile(r'(\d+)\s+PENDING')
    alphas = []

    with hopeful_lock:
        if os.path.exists(HOPEFUL_ALPHAS_FILE):
            try:
                if os.path.isfile(HOPEFUL_ALPHAS_FILE) and os.path.getsize(HOPEFUL_ALPHAS_FILE) > 0:
                    with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f: content = f.read()
                    if content:
                        alphas_data = json.loads(content)
                        if isinstance(alphas_data, list): alphas = alphas_data; logger.info(f"[Stats] Loaded {len(alphas)} alphas from hopeful_alphas.json")
                        else: logger.warning(f"[Stats] hopeful_alphas.json did not contain a list. Found type: {type(alphas_data)}")
            except (IOError, json.JSONDecodeError) as e: logger.error(f"[Stats] Error processing {HOPEFUL_ALPHAS_FILE}: {e}")
            except Exception as e: logger.error(f"[Stats] Unexpected error reading {HOPEFUL_ALPHAS_FILE}: {e}", exc_info=True)

    if alphas:
        try:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list)

            all_fitness = [a.get('performance', {}).get('fitness') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]
            all_sharpe = [a.get('performance', {}).get('sharpe') for a in valid_alphas_list if isinstance(a.get('performance'), dict)]

            valid_fitness = [float(f) for f in all_fitness if isinstance(f, (int, float, str)) and (str(f).replace('.', '', 1).isdigit() or str(f).replace('-', '', 1).replace('.', '', 1).isdigit())]
            valid_sharpe = [float(s) for s in all_sharpe if isinstance(s, (int, float, str)) and (str(s).replace('.', '', 1).isdigit() or str(s).replace('-', '', 1).replace('.', '', 1).isdigit())]

            if valid_fitness:
                 stats['max_fitness'] = max(valid_fitness) if valid_fitness else 0.0
                 stats['avg_fitness'] = sum(valid_fitness) / len(valid_fitness) if valid_fitness else 0.0
            if valid_sharpe:
                 stats['max_sharpe'] = max(valid_sharpe) if valid_sharpe else 0.0

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
                try: fitness_f = float(fitness)
                except (ValueError, TypeError): fitness_f = -999
                try: sharpe_f = float(sharpe)
                except (ValueError, TypeError): sharpe_f = 0.0
                try: turnover_f = float(turnover)
                except (ValueError, TypeError): turnover_f = 1.0
                return fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1)

            processed_alphas_temp = []
            processed_count = 0
            # v6.1.5: Initialize new counters
            successfully_submitted_count_local = 0
            total_submitted_count_local = 0

            for alpha_report in valid_alphas_list:
                try:
                    expression = alpha_report.get('expression')
                    if not expression: continue
                    perf_data = alpha_report.get('performance', {});
                    if not isinstance(perf_data, dict): perf_data = {}
                    summary_str = alpha_report.get('checks_summary', '') or ''
                    fail_match = fail_pattern.search(summary_str); has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
                    pending_match = pending_pattern.search(summary_str); has_pending = bool(pending_match and int(pending_match.group(1)) > 0)
                    pass_match = pass_pattern.search(summary_str); passed_count = int(pass_match.group(1)) if pass_match else 0
                    is_submittable = passed_count >= 7 and not has_fail
                    is_submitted = expression in submitted_set
                    is_failed_on_wq = expression in failed_set
                    is_successfully_submitted = is_submittable and is_submitted and not is_failed_on_wq

                    if is_submittable and not is_submitted and not is_failed_on_wq: stats['submittable_pending_count'] += 1

                    # v6.1.5: Increment submission counts
                    if is_submitted:
                         total_submitted_count_local += 1
                         if is_successfully_submitted: # Only count as successful if also submittable and not failed
                             successfully_submitted_count_local += 1

                    processed_alpha_data = {
                        "expression": expression, "timestamp": alpha_report.get('timestamp', 'N/A'),
                        "checks_summary": summary_str, "is_submittable": is_submittable,
                        "is_submitted": is_submitted, "is_failed_on_wq": is_failed_on_wq,
                        "is_successfully_submitted": is_successfully_submitted,
                        "dashboard_score": calculate_dashboard_score(alpha_report),
                        "performance": perf_data
                    }
                    processed_alphas_temp.append(processed_alpha_data)
                    processed_count += 1
                except Exception as e: logger.error(f"[Stats Process] Error processing alpha: {alpha_report.get('expression', 'N/A')}. Error: {e}", exc_info=True)

            logger.info(f"[Stats] Processed {processed_count}/{len(valid_alphas_list)} valid alphas for stats.")
            stats['all_alphas'] = processed_alphas_temp
            # v6.1.5: Assign calculated counts to stats dict
            stats['successfully_submitted_count'] = successfully_submitted_count_local
            stats['total_submitted_count'] = total_submitted_count_local

        except Exception as e: logger.error(f"[Stats] Unexpected error processing alphas list: {e}", exc_info=True)
    return stats
# --- v6.1.5 End Fix ---


# --- get_version_from_file remains unchanged ---
def get_version_from_file(file_path, version_regex_str):
    # ... (代码不变) ...
    logger.info(f"[Version] Attempting to read version from {file_path}")
    version_regex = re.compile(version_regex_str)
    try:
        if not os.path.isfile(file_path):
            logger.error(f"[Version] File not found: {file_path}")
            return "file_not_found"
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        match = version_regex.search(content)
        if match:
            version = match.group(1); logger.info(f"[Version] Found version {version} in {file_path}"); return version
        else: logger.warning(f"[Version] Regex did not find version in {file_path}"); return "unknown_format"
    except IOError as e: logger.error(f"[Version] IOError reading {file_path}: {e}"); return "read_error"
    except Exception as e: logger.error(f"[Version] Unexpected error reading {file_path}: {e}"); return "read_error"

# --- Main Routes ---
@app.route('/')
def dashboard():
    # v10.0: Add chart_page link variable
    return render_template('dashboard_v4.html', settings_page=True, chart_page=True)

@app.route('/settings')
def settings_page():
    logger.info("[API /settings] Request received for settings page.")
    return render_template('settings.html')

# --- v10.0: New Route for Chart Page ---
@app.route('/chart')
def chart_page():
    logger.info("[API /chart] Request received for chart page.")
    return render_template('chart.html') # Assumes chart.html exists in templates
# --- v10.0 End ---


# --- API Routes ---
@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    # ... (代码不变) ...
    logger.info("[API /api/get_settings] Request received.")
    with config_lock:
        try:
            if not os.path.exists(SYSTEM_CONFIG_FILE):
                logger.error(f"[API /api/get_settings] {SYSTEM_CONFIG_FILE} not found!")
                return jsonify({"error": "Config file not found on server."}), 404
            
            with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
            response = make_response(jsonify(data))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response
        except json.JSONDecodeError as e:
            logger.error(f"[API /api/get_settings] Error decoding config file {SYSTEM_CONFIG_FILE}: {e}", exc_info=True)
            return jsonify({"error": f"Config file is corrupted: {e}"}), 500
        except Exception as e:
            logger.error(f"[API /api/get_settings] Error reading config file: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    # ... (代码不变) ...
    logger.info("[API /api/save_settings] Request received.")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    
    new_config = request.json
    expected_keys = {
        "wq_api_cooldown": int, "llm_api_cooldown": int,
        "miner_concurrency": int, "miner_sleep": int,
        "evolver_concurrency": int, "evolver_sleep": int,
        "producer_queue_full_sleep": int
    }
    if not isinstance(new_config, dict): return jsonify(status='error', message='Invalid JSON format (must be an object)'), 400

    validated_config = {}
    with config_lock:
        try:
            if os.path.exists(SYSTEM_CONFIG_FILE):
                try:
                    with open(SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f: validated_config = json.load(f)
                except json.JSONDecodeError:
                    logger.warning(f"Existing {SYSTEM_CONFIG_FILE} is corrupt, will overwrite.")
                    validated_config = {}

            for key, expected_type in expected_keys.items():
                if key not in new_config:
                    if key not in validated_config and key in expected_keys:
                        return jsonify(status='error', message=f"Missing key: {key}"), 400
                    continue

                value = new_config[key]
                try:
                    converted_value = expected_type(value)
                    if converted_value < 0: return jsonify(status='error', message=f"{key} must be >= 0"), 400
                    validated_config[key] = converted_value
                except (ValueError, TypeError):
                     return jsonify(status='error', message=f"Invalid type for {key}. Expected {expected_type.__name__}, got '{value}'"), 400

            if 'evolver_search_space' in new_config and isinstance(new_config['evolver_search_space'], dict):
                validated_config['evolver_search_space'] = new_config['evolver_search_space']
            elif 'evolver_search_space' not in validated_config:
                validated_config['evolver_search_space'] = {}

            with open(SYSTEM_CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(validated_config, f, indent=2)
            logger.info(f"[API /api/save_settings] Successfully saved new config: {validated_config}")
            global _timeseries_cache, _timeseries_cache_time # Invalidate cache on settings save
            with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None; logger.info("[API /api/save_settings] Timeseries cache invalidated.")
            return jsonify(status='success', message='Config saved')

        except Exception as e:
            logger.error(f"[API /api/save_settings] Error saving config file: {e}", exc_info=True)
            return jsonify(status='error', message=f"Internal server error: {e}"), 500

@app.route('/status')
def status():
    # ... (代码不变) ...
    logger.info("[API /status] Request received.")
    try:
        data = { "miner": get_service_status('miner.log'),
                 "evolver": get_service_status('evolver.log'),
                 "hopeful_alphas": get_hopeful_alphas_stats() } # This now includes submission counts
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
        logger.info("[API /status] Request completed successfully."); return response
    except Exception as e: logger.critical(f"[API /status] CRITICAL Error: {e}", exc_info=True); return jsonify({"error": "Failed to retrieve status data due to an internal server error."}), 500

@app.route('/api/version_info')
def version_info():
    # ... (代码不变) ...
    logger.info("[API /version_info] Request received.")
    dashboard_version = CURRENT_DASHBOARD_VERSION
    generator_version = get_version_from_file(GENERATOR_FILE_PATH, r'CURRENT_GENERATOR_VERSION\s*=\s*["\'](v[0-9]+\.[0-9]+\.[^"\']*)["\']')
    data = {"dashboard_version": dashboard_version, "generator_version": generator_version}
    response = make_response(jsonify(data)); response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; return response

# --- v6.1.4: Updated API Endpoint with corrected Fitness Mean & round order ---
@app.route('/api/v1/stats/timeseries')
def api_stats_timeseries():
    # ... (代码不变) ...
    global _timeseries_cache, _timeseries_cache_time
    logger.info("[API /api/v1/stats/timeseries] Request received.")

    with _cache_lock:
        now = datetime.now(timezone.utc)
        if _timeseries_cache and _timeseries_cache_time and (now - _timeseries_cache_time < CACHE_DURATION):
            logger.info("[API Timeseries] Returning cached data.")
            return jsonify(_timeseries_cache)

        logger.info("[API Timeseries] Cache invalid or expired. Generating new data...")
        data = []
        with tested_log_lock:
            if not os.path.exists(TESTED_ALPHAS_LOG_FILE):
                logger.warning(f"[API Timeseries] File not found: {TESTED_ALPHAS_LOG_FILE}")
                return jsonify({"error": "Log file not found."}), 404
            try:
                df = pd.read_json(TESTED_ALPHAS_LOG_FILE)
                if df.empty:
                    logger.info("[API Timeseries] Log file is empty.")
                    _timeseries_cache = {"timestamps": [], "count": [], "mean_fitness": [], "high_quality_count": []}
                    _timeseries_cache_time = now
                    return jsonify(_timeseries_cache)

                df['timestamp_dt'] = pd.to_datetime(df['timestamp'], errors='coerce')
                df = df.dropna(subset=['timestamp_dt'])
                df['fitness_num'] = pd.to_numeric(df['fitness'], errors='coerce') # Keep NaN
                df_valid_fitness = df.dropna(subset=['fitness_num'])
                df['passed_checks_num'] = pd.to_numeric(df['passed_checks'], errors='coerce').fillna(0)
                df = df.set_index('timestamp_dt')
                df_valid_fitness = df_valid_fitness.set_index('timestamp_dt')

                # v6.1.4: Use 'h'
                resampler_all = df.resample('h')
                resampler_valid = df_valid_fitness.resample('h')

                agg_counts = resampler_all['expression'].size()
                agg_mean_fitness = resampler_valid['fitness_num'].mean() # Skips NaN by default
                df_hq = df[(df['fitness_num'] > 0.5) & (df['passed_checks_num'] >= 4)]
                hq_counts = df_hq.resample('h').size()

                combined_index = agg_counts.index.union(agg_mean_fitness.index).union(hq_counts.index)
                output_df = pd.DataFrame(index=combined_index)
                output_df['count'] = agg_counts.reindex(combined_index).fillna(0).astype(int)
                output_df['mean_fitness'] = agg_mean_fitness.reindex(combined_index) # Keep NaN
                output_df['high_quality_count'] = hq_counts.reindex(combined_index).fillna(0).astype(int)

                output = {
                    "timestamps": output_df.index.strftime('%Y-%m-%dT%H:%M:%S').tolist(),
                    "count": output_df['count'].tolist(),
                    "mean_fitness": output_df['mean_fitness'].round(4).replace({np.nan: None}).tolist(), # Round then replace NaN
                    "high_quality_count": output_df['high_quality_count'].tolist()
                }

                _timeseries_cache = output
                _timeseries_cache_time = now
                logger.info(f"[API Timeseries] Successfully processed {len(df)} log entries. Cached result.")
                return jsonify(output)

            except json.JSONDecodeError as e:
                logger.error(f"[API Timeseries] Error decoding JSON from {TESTED_ALPHAS_LOG_FILE}: {e}")
                with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                return jsonify({"error": "Log file is corrupted."}), 500
            except FileNotFoundError:
                 logger.error(f"[API Timeseries] File disappeared: {TESTED_ALPHAS_LOG_FILE}")
                 with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                 return jsonify({"error": "Log file not found."}), 404
            except Exception as e:
                logger.error(f"[API Timeseries] Error processing log file: {e}", exc_info=True)
                with _cache_lock: _timeseries_cache = None; _timeseries_cache_time = None;
                return jsonify({"error": "Internal server error processing log file."}), 500
# --- v6.1.4 End ---


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    # ... (代码不变) ...
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files: return "Invalid log file requested", 404
    try: return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError: return f"Log file '{log_filename}' not found.", 404
    except Exception as e: logger.error(f"[API /download_logs] Error: {e}"); return "Error downloading file", 500

# --- Mark/Unmark API routes remain unchanged ---
@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    # ... (代码不变) ...
    operation = "Mark"; logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='Invalid/missing expression'), 400
    logger.info(f"[API /{operation.lower()}_submitted] Expr: {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas(); original_size = len(submitted_set); submitted_set.add(expression); new_size = len(submitted_set)
        if save_submitted_alphas(submitted_set): logger.info(f"[API /{operation.lower()}_submitted] Success."); return jsonify(status='success', message=operation+'ed')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='Failed to save status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    # ... (代码不变) ...
    operation = "Unmark"; logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='Invalid/missing expression'), 400
    logger.info(f"[API /{operation.lower()}_submitted] Expr: {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas(); original_size = len(submitted_set); submitted_set.discard(expression); new_size = len(submitted_set)
        if save_submitted_alphas(submitted_set): logger.info(f"[API /{operation.lower()}_submitted] Success."); return jsonify(status='success', message=operation+'ed')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save failed."); return jsonify(status='error', message='Failed to save status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error'), 500

@app.route('/api/mark_failed_on_wq', methods=['POST'])
def mark_alpha_failed():
    # ... (代码不变) ...
    operation = "MarkFailed"; logger.info(f"[API /{operation.lower()}] Request received.")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='Invalid/missing expression'), 400
    logger.info(f"[API /{operation.lower()}] Expr: {expression[:50]}...")
    try:
        failed_set = load_failed_submissions(); original_size = len(failed_set); failed_set.add(expression); new_size = len(failed_set)
        if save_failed_submissions(failed_set): logger.info(f"[API /{operation.lower()}] Success."); return jsonify(status='success', message='WQ Failed status Marked')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='Failed to save status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] CRITICAL: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error'), 500

@app.route('/api/unmark_failed_on_wq', methods=['POST'])
def unmark_alpha_failed():
    # ... (代码不变) ...
    operation = "UnmarkFailed"; logger.info(f"[API /{operation.lower()}] Request received.")
    if not request.is_json: return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): return jsonify(status='error', message='Invalid/missing expression'), 400
    logger.info(f"[API /{operation.lower()}] Expr: {expression[:50]}...")
    try:
        failed_set = load_failed_submissions(); original_size = len(failed_set); failed_set.discard(expression); new_size = len(failed_set)
        if save_failed_submissions(failed_set): logger.info(f"[API /{operation.lower()}] Success."); return jsonify(status='success', message='WQ Failed status Unmarked')
        else: logger.error(f"[API /{operation.lower()}] Save failed."); return jsonify(status='error', message='Failed to save status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] CRITICAL: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error'), 500

# --- Main execution block remains unchanged ---
if __name__ == '__main__':
    # ... (代码不变) ...
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR); logger.info(f"Created log directory: {LOG_DIR}")
        except OSError as e: logger.error(f"Error creating log directory {LOG_DIR}: {e}")
    try: os.stat_cache.clear(); logger.info("Cleared os.stat_cache() on startup.")
    except AttributeError: pass
    logger.info(f"Starting Flask application (Version: {CURRENT_DASHBOARD_VERSION})...")
    try:
        import pandas
        logger.info(f"Pandas version {pandas.__version__} detected.")
    except ImportError:
         logger.warning("Pandas library not found. Timeseries API will not work until installed (pip install pandas).")

    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)