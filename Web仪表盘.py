# --- Web仪表盘.py v5.8 (Show Version Info) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re # v5.8: 确保导入 re
import threading
from datetime import datetime, timedelta
from collections import deque
import os.path
import logging

# --- v5.8: 版本号 ---
CURRENT_DASHBOARD_VERSION = "v5.8"
# --- v5.8: 结束 ---

# --- 配置基础日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
FAILED_SUBMISSIONS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'failed_submissions.json'))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
# --- v5.8: 新增文件路径 ---
# (这些路径是在容器内的路径，假设 generator 文件被挂载到同级)
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")
DASHBOARD_FILE_PATH = os.path.join(BASE_DIR, "Web仪表盘.py") # 指向自身
# --- v5.8: 结束 ---


# v5.8: 不再需要 FILES_TO_TRACK 字典，改为直接读取
# FILES_TO_TRACK = { ... } # 已删除

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
file_lock = threading.Lock() # 用于 submitted_alphas.json
hopeful_lock = threading.Lock() # 用于 hopeful_alphas.json
failed_lock = threading.Lock() # 用于 failed_submissions.json

# --- 加强日志: load_submitted_alphas (保持 v5.3) ---
def load_submitted_alphas():
    # ... (代码不变) ...
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath): logger.info(f"[Submit Load] File not found: {filepath}. Returning empty set."); return set()
        try:
            if os.path.getsize(filepath) < 2: logger.info(f"[Submit Load] File is empty or too small: {filepath}. Returning empty set."); return set()
            with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, (list, set)):
                loaded_set = set(data); sample = list(loaded_set)[:3]
                logger.info(f"[Submit Load] Successfully loaded {len(loaded_set)} items from {filepath}. Sample: {sample}"); return loaded_set
            else: logger.warning(f"[Submit Load] File {filepath} did not contain a list/set. Found type: {type(data)}. Returning empty set."); return set()
        except json.JSONDecodeError as e: logger.error(f"[Submit Load] Error decoding JSON from {filepath}: {e}. Returning empty set."); return set()
        except IOError as e: logger.error(f"[Submit Load] IOError reading {filepath}: {e}. Returning empty set."); return set()
        except Exception as e: logger.error(f"[Submit Load] Unexpected error loading {filepath}: {e}", exc_info=True); return set()

# --- 加强日志: save_submitted_alphas (保持 v5.3) ---
def save_submitted_alphas(submitted_set):
    # ... (代码不变) ...
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        logger.info(f"[Submit Save] Attempting to save {len(submitted_set)} items to {filepath}")
        try:
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(submitted_set), f, indent=4)
            logger.info(f"[Submit Save] Successfully saved {len(submitted_set)} items to {filepath}"); return True
        except IOError as e: logger.error(f"[Submit Save] IOError saving {filepath}: {e}"); return False
        except Exception as e: logger.error(f"[Submit Save] Unexpected error saving {filepath}: {e}", exc_info=True); return False

# --- 加载/保存 failed_submissions 的函数 (保持 v5.6) ---
def load_failed_submissions():
    # ... (代码不变) ...
    with failed_lock:
        filepath = FAILED_SUBMISSIONS_FILE
        if not os.path.exists(filepath): logger.info(f"[Failed Load] File not found: {filepath}. Returning empty set."); return set()
        try:
            if os.path.getsize(filepath) < 2: logger.info(f"[Failed Load] File is empty or too small: {filepath}. Returning empty set."); return set()
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
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(list(failed_set), f, indent=4)
            logger.info(f"[Failed Save] Successfully saved {len(failed_set)} items to {filepath}"); return True
        except IOError as e: logger.error(f"[Failed Save] IOError saving {filepath}: {e}"); return False
        except Exception as e: logger.error(f"[Failed Save] Unexpected error saving {filepath}: {e}", exc_info=True); return False

# ... (get_service_status 保持不变) ...
def get_service_status(log_file):
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

# --- v5.7: 优化 get_hopeful_alphas_stats (加入 is_successfully_submitted 标志) ---
def get_hopeful_alphas_stats():
    stats = {
        "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
        "submittable_pending_count": 0, "all_alphas": []
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
                # ... (加载 hopeful_alphas.json 的逻辑不变) ...
                if os.path.getsize(HOPEFUL_ALPHAS_FILE) > 0:
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

            # ... (计算 max/avg fitness/sharpe 的逻辑不变) ...
            all_fitness = [a.get('performance', {}).get('fitness', 0) for a in valid_alphas_list if isinstance(a.get('performance'), dict)]
            all_sharpe = [a.get('performance', {}).get('sharpe', 0) for a in valid_alphas_list if isinstance(a.get('performance'), dict)]

            # v5.8: 过滤 None 或非数字
            valid_fitness = [f for f in all_fitness if isinstance(f, (int, float))]
            valid_sharpe = [s for s in all_sharpe if isinstance(s, (int, float))]

            if valid_fitness:
                 stats['max_fitness'] = max(valid_fitness) if valid_fitness else 0.0
                 stats['avg_fitness'] = sum(valid_fitness) / len(valid_fitness) if valid_fitness else 0.0
            if valid_sharpe:
                 stats['max_sharpe'] = max(valid_sharpe) if valid_sharpe else 0.0


            def calculate_combined_score(report):
                # ... (此函数逻辑不变, 但稍作清理) ...
                if not isinstance(report, dict): return -float('inf')
                perf = report.get('performance', {})
                if not isinstance(perf, dict): return -float('inf') # v5.8: 增加检查

                fitness = perf.get('fitness', -999)
                sharpe = perf.get('sharpe', 0.0)
                turnover = perf.get('turnover', 1.0)
                checks_summary = report.get('checks_summary', '0 PASS')
                passed_count = 0

                try: # v5.8: 修正正则匹配
                    match = re.search(r'(\d+)\s+PASS', checks_summary or '')
                    if match: passed_count = int(match.group(1))
                except (ValueError, TypeError): pass # 保持 0

                try: fitness_f = float(fitness)
                except (ValueError, TypeError): fitness_f = -999
                try: sharpe_f = float(sharpe)
                except (ValueError, TypeError): sharpe_f = 0.0
                try: turnover_f = float(turnover)
                except (ValueError, TypeError): turnover_f = 1.0

                return fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1)


            processed_alphas_temp = []
            processed_count = 0

            for alpha in valid_alphas_list:
                try:
                    expression = alpha.get('expression')
                    if not expression: continue

                    perf_data = alpha.get('performance', {})
                    # v5.8: 健壮性检查
                    if not isinstance(perf_data, dict): perf_data = {}
                    fitness_val = perf_data.get('fitness', 0)
                    summary = alpha.get('checks_summary', '')
                    summary_str = summary if summary is not None else ''

                    fail_match = fail_pattern.search(summary_str); has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
                    pending_match = pending_pattern.search(summary_str); has_pending = bool(pending_match and int(pending_match.group(1)) > 0)
                    pass_match = pass_pattern.search(summary_str); passed_count = int(pass_match.group(1)) if pass_match else 0

                    is_all_pass = passed_count >= 7 and not has_fail and not has_pending
                    is_submittable = passed_count >= 7 and not has_fail
                    is_submitted = bool(expression in submitted_set)
                    is_failed_on_wq = bool(expression in failed_set)

                    # --- v5.7: 计算新标志 ---
                    is_successfully_submitted = is_submittable and is_submitted and not is_failed_on_wq
                    # --- v5.7 结束 ---

                    if is_submittable and not is_submitted and not is_failed_on_wq:
                        stats['submittable_pending_count'] += 1

                    processed_alphas_temp.append({
                        "expression": expression, "fitness": fitness_val, "sharpe": perf_data.get('sharpe', 0),
                        "turnover": perf_data.get('turnover', 0), "returns": perf_data.get('returns', 0),
                        "checks": summary_str, "timestamp": alpha.get('timestamp', 'N/A'),
                        "is_all_pass": is_all_pass, "is_submittable": is_submittable,
                        "is_submitted": is_submitted, "is_failed_on_wq": is_failed_on_wq,
                        "is_successfully_submitted": is_successfully_submitted, # <-- v5.7: 传递给前端
                        "combined_score": calculate_combined_score(alpha)
                    })
                    processed_count += 1
                except Exception as e: logger.error(f"[Stats Process] Error processing alpha: {alpha.get('expression', 'N/A')}. Error: {e}", exc_info=True)

            logger.info(f"[Stats] Processed {processed_count}/{len(valid_alphas_list)} valid alphas for stats.")

            def sort_key(alpha):
                # ... (排序逻辑保持 v5.4 不变) ...
                # v5.8: 健壮性检查
                fitness_val = -float('inf')
                if isinstance(alpha.get('fitness'), (int, float)):
                    fitness_val = alpha['fitness']
                sort_fitness = (fitness_val >= 1)
                sort_submitted = (not alpha.get('is_submitted', False)) # 默认 False
                sort_combined = alpha.get('combined_score', -float('inf')) # 默认最低
                return (sort_fitness, sort_submitted, sort_combined)

            processed_alphas_temp.sort(key=sort_key, reverse=True)
            stats['all_alphas'] = processed_alphas_temp

        except Exception as e: logger.error(f"[Stats] Unexpected error processing alphas list: {e}", exc_info=True)

    return stats
# --- v5.7 结束 ---

# v5.8: 不再使用 get_file_versions
# def get_file_versions(): ... # 已删除

# --- v5.8: 新增辅助函数 - 从文件读取版本号 ---
def get_version_from_file(file_path, version_regex_str):
    logger.info(f"[Version] Attempting to read version from {file_path}")
    version_regex = re.compile(version_regex_str)
    try:
        # 使用'r'模式，因为我们挂载的是 .py 文件，而不是 .pyc
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        match = version_regex.search(content)
        if match:
            version = match.group(1)
            logger.info(f"[Version] Found version {version} in {file_path}")
            return version
        else:
            logger.warning(f"[Version] Regex did not find version in {file_path}")
            return "unknown_format"
    except IOError as e:
        logger.error(f"[Version] IOError reading {file_path}: {e}")
        return "file_not_found"
    except Exception as e:
        logger.error(f"[Version] Unexpected error reading {file_path}: {e}")
        return "read_error"
# --- v5.8: 结束 ---


@app.route('/')
def dashboard(): return render_template('dashboard_v4.html')

# ... ( /status 路由保持不变, 但调用 get_file_versions 已删除) ...
@app.route('/status')
def status():
    logger.info("[API /status] Request received.")
    try:
        # v5.8: 移除 file_versions 的调用
        data = { "miner": get_service_status('miner.log'), "evolver": get_service_status('evolver.log'),
                 "hopeful_alphas": get_hopeful_alphas_stats()
                 # "file_versions": get_file_versions() # <-- 已删除
               }
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
        logger.info("[API /status] Request completed successfully with no-cache headers."); return response
    except Exception as e: logger.critical(f"[API /status] CRITICAL Error: {e}", exc_info=True); return jsonify({"error": "Failed to retrieve status data due to an internal server error."}), 500

# --- v5.8: 新增 API - 获取版本信息 ---
@app.route('/api/version_info')
def version_info():
    logger.info("[API /version_info] Request received.")

    # 1. Dashboard 版本 (直接读取常量)
    dashboard_version = CURRENT_DASHBOARD_VERSION

    # 2. Generator 版本 (读取挂载的文件)
    generator_version = get_version_from_file(
        GENERATOR_FILE_PATH,
        r'CURRENT_GENERATOR_VERSION\s*=\s*["\'](v[0-9]+\.[0-9]+\.[0-9]+[^"\']*)["\']'
    )

    data = {
        "dashboard_version": dashboard_version,
        "generator_version": generator_version
    }
    response = make_response(jsonify(data))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'; response.headers['Pragma'] = 'no-cache'; response.headers['Expires'] = '0'
    logger.info("[API /version_info] Request completed successfully.")
    return response
# --- v5.8: 结束 ---


# ... ( /download_logs 保持不变) ...
@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files: logger.warning(f"[API /download_logs] Invalid log file requested: {log_filename}"); return "Invalid log file requested", 404
    try: logger.info(f"[API /download_logs] Serving file: {log_filename}"); return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError: logger.error(f"[API /download_logs] File not found: {log_filename}"); return f"Log file '{log_filename}' not found in '{LOG_DIR}/' directory.", 404
    except Exception as e: logger.error(f"[API /download_logs] Error downloading log {log_filename}: {e}", exc_info=True); return "Error downloading file", 500

# ... ( /api/mark_submitted 和 /api/unmark_submitted 保持不变) ...
@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    operation = "Mark"; logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json: logger.warning(f"[API /{operation.lower()}_submitted] Request is not JSON."); return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): logger.warning(f"[API /{operation.lower()}_submitted] Invalid or missing expression."); return jsonify(status='error', message='Invalid or missing expression'), 400
    logger.info(f"[API /{operation.lower()}_submitted] Received expression (first 50 chars): {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas(); original_size = len(submitted_set); submitted_set.add(expression); new_size = len(submitted_set)
        if new_size > original_size: logger.info(f"[API /{operation.lower()}_submitted] Expression added to set. Attempting save.")
        else: logger.info(f"[API /{operation.lower()}_submitted] Expression already in set. Attempting save (idempotent).")
        if save_submitted_alphas(submitted_set): logger.info(f"[API /{operation.lower()}_submitted] Operation successful for expression: {expression[:50]}..."); return jsonify(status='success', message=operation+'ed')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save operation failed for expression: {expression[:50]}..."); return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL Error: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error while {operation.lower()}ing'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    operation = "Unmark"; logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json: logger.warning(f"[API /{operation.lower()}_submitted] Request is not JSON."); return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): logger.warning(f"[API /{operation.lower()}_submitted] Invalid or missing expression."); return jsonify(status='error', message='Invalid or missing expression'), 400
    logger.info(f"[API /{operation.lower()}_submitted] Received expression (first 50 chars): {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas(); original_size = len(submitted_set); submitted_set.discard(expression); new_size = len(submitted_set)
        if new_size < original_size: logger.info(f"[API /{operation.lower()}_submitted] Expression removed from set. Attempting save.")
        else: logger.info(f"[API /{operation.lower()}_submitted] Expression was not in set. Attempting save (idempotent).")
        if save_submitted_alphas(submitted_set): logger.info(f"[API /{operation.lower()}_submitted] Operation successful for expression: {expression[:50]}..."); return jsonify(status='success', message=operation+'ed')
        else: logger.error(f"[API /{operation.lower()}_submitted] Save operation failed for expression: {expression[:50]}..."); return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL Error: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error while {operation.lower()}ing'), 500

# ... ( /api/mark_failed_on_wq 和 /api/unmark_failed_on_wq 保持不变) ...
@app.route('/api/mark_failed_on_wq', methods=['POST'])
def mark_alpha_failed():
    operation = "MarkFailed"; logger.info(f"[API /{operation.lower()}] Request received.")
    if not request.is_json: logger.warning(f"[API /{operation.lower()}] Request is not JSON."); return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): logger.warning(f"[API /{operation.lower()}] Invalid or missing expression."); return jsonify(status='error', message='Invalid or missing expression'), 400
    logger.info(f"[API /{operation.lower()}] Received expression (first 50 chars): {expression[:50]}...")
    try:
        failed_set = load_failed_submissions(); original_size = len(failed_set); failed_set.add(expression); new_size = len(failed_set)
        if new_size > original_size: logger.info(f"[API /{operation.lower()}] Expression added to failed set. Attempting save.")
        else: logger.info(f"[API /{operation.lower()}] Expression already in failed set. Attempting save (idempotent).")
        if save_failed_submissions(failed_set): logger.info(f"[API /{operation.lower()}] Operation successful for expression: {expression[:50]}..."); return jsonify(status='success', message='WQ Failed status Marked')
        else: logger.error(f"[API /{operation.lower()}] Save operation failed for expression: {expression[:50]}..."); return jsonify(status='error', message='Failed to save WQ Failed status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] CRITICAL Error: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error while marking WQ Failed'), 500

@app.route('/api/unmark_failed_on_wq', methods=['POST'])
def unmark_alpha_failed():
    operation = "UnmarkFailed"; logger.info(f"[API /{operation.lower()}] Request received.")
    if not request.is_json: logger.warning(f"[API /{operation.lower()}] Request is not JSON."); return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json; expression = data.get('expression')
    if not expression or not isinstance(expression, str): logger.warning(f"[API /{operation.lower()}] Invalid or missing expression."); return jsonify(status='error', message='Invalid or missing expression'), 400
    logger.info(f"[API /{operation.lower()}] Received expression (first 50 chars): {expression[:50]}...")
    try:
        failed_set = load_failed_submissions(); original_size = len(failed_set); failed_set.discard(expression); new_size = len(failed_set)
        if new_size < original_size: logger.info(f"[API /{operation.lower()}] Expression removed from failed set. Attempting save.")
        else: logger.info(f"[API /{operation.lower()}] Expression was not in set. Attempting save (idempotent).")
        if save_failed_submissions(failed_set): logger.info(f"[API /{operation.lower()}] Operation successful for expression: {expression[:50]}..."); return jsonify(status='success', message='WQ Failed status Unmarked')
        else: logger.error(f"[API /{operation.lower()}] Save operation failed for expression: {expression[:50]}..."); return jsonify(status='error', message='Failed to save WQ Failed status'), 500
    except Exception as e: logger.critical(f"[API /{operation.lower()}] CRITICAL Error: {e}", exc_info=True); return jsonify(status='error', message=f'Internal server error while unmarking WQ Failed'), 500

if __name__ == '__main__':
    # ... (启动逻辑不变) ...
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR); logger.info(f"Created log directory: {LOG_DIR}")
        except OSError as e: logger.error(f"Error creating log directory {LOG_DIR}: {e}")
    try: os.stat_cache.clear(); logger.info("Cleared os.stat_cache() on startup.")
    except AttributeError: logger.info("os.stat_cache() not available on this platform, skipping.")
    logger.info(f"Starting Flask application (Version: {CURRENT_DASHBOARD_VERSION})...") # v5.8: 显示版本
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)