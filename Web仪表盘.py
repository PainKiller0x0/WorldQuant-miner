# --- Web仪表盘.py v5.4 (Advanced Sorting Logic) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import threading
from datetime import datetime, timedelta
from collections import deque
import os.path
import logging 

# --- 配置基础日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) 

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'submitted_alphas.json'))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

FILES_TO_TRACK = {
    "仪表盘后端 (Py)": os.path.abspath(os.path.join(BASE_DIR, "Web仪表盘.py")),
    "仪表盘前端 (HTML)": os.path.abspath(os.path.join(TEMPLATE_DIR, "dashboard_v4.html")),
    "核心生成器 (Py)": os.path.abspath(os.path.join(BASE_DIR, "alpha_generator_ollama.py"))
}

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
file_lock = threading.Lock()
hopeful_lock = threading.Lock()

# --- v5.2-debug: 保留加强日志: load_submitted_alphas ---
def load_submitted_alphas():
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        if not os.path.exists(filepath):
            logger.info(f"[Submit Load] File not found: {filepath}. Returning empty set.")
            return set()
        try:
            if os.path.getsize(filepath) < 2:
                logger.info(f"[Submit Load] File is empty or too small: {filepath}. Returning empty set.")
                return set()

            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, (list, set)):
                    loaded_set = set(data)
                    sample = list(loaded_set)[:3]
                    logger.info(f"[Submit Load] Successfully loaded {len(loaded_set)} items from {filepath}. Sample: {sample}")
                    return loaded_set
                else:
                    logger.warning(f"[Submit Load] File {filepath} did not contain a list/set. Found type: {type(data)}. Returning empty set.")
                    return set()
        except json.JSONDecodeError as e:
            logger.error(f"[Submit Load] Error decoding JSON from {filepath}: {e}. Returning empty set.")
            return set()
        except IOError as e:
            logger.error(f"[Submit Load] IOError reading {filepath}: {e}. Returning empty set.")
            return set()
        except Exception as e: 
            logger.error(f"[Submit Load] Unexpected error loading {filepath}: {e}", exc_info=True)
            return set()
# --- 日志加强结束 ---

# --- v5.2-debug: 保留加强日志: save_submitted_alphas ---
def save_submitted_alphas(submitted_set):
    with file_lock:
        filepath = SUBMITTED_ALPHAS_FILE
        logger.info(f"[Submit Save] Attempting to save {len(submitted_set)} items to {filepath}")
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(list(submitted_set), f, indent=4)
            logger.info(f"[Submit Save] Successfully saved {len(submitted_set)} items to {filepath}")
            return True
        except IOError as e:
            logger.error(f"[Submit Save] IOError saving {filepath}: {e}")
            return False
        except Exception as e: 
            logger.error(f"[Submit Save] Unexpected error saving {filepath}: {e}", exc_info=True)
            return False
# --- 日志加强结束 ---


def get_service_status(log_file):
    status = "UNKNOWN"
    last_seen = "Never"
    logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)

    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')

            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT:
                status = "RUNNING"
            else:
                status = "STALLED"

            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                latest_lines_deque = deque(f, maxlen=50)

            latest_lines = list(latest_lines_deque)
            latest_lines.reverse()
            logs = "".join(latest_lines)

        except Exception as e:
            logs = f"Error reading log file: {e}"
            status = "ERROR"
            logger.error(f"Error getting service status for {log_file}: {e}", exc_info=True)
    else:
        status = "NOT FOUND"

    return {"status": status, "last_seen": last_seen, "logs": logs}

# --- v5.4: 优化 get_hopeful_alphas_stats (高级排序) ---
def get_hopeful_alphas_stats():
    stats = {
        "count": 0,
        "max_fitness": 0.0,
        "max_sharpe": 0.0,
        "avg_fitness": 0.0,
        "submittable_pending_count": 0, 
        "all_alphas": []
    }
    submitted_set = load_submitted_alphas()
    logger.info(f"[Stats] Loaded submitted set with {len(submitted_set)} items for stats calculation.")

    pass_pattern = re.compile(r'(\d+)\s+PASS')
    fail_pattern = re.compile(r'(\d+)\s+FAIL')
    pending_pattern = re.compile(r'(\d+)\s+PENDING')
    alphas = []

    with hopeful_lock:
        if os.path.exists(HOPEFUL_ALPHAS_FILE):
            try:
                if os.path.getsize(HOPEFUL_ALPHAS_FILE) > 0:
                    with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content:
                           alphas_data = json.loads(content)
                           if isinstance(alphas_data, list):
                               alphas = alphas_data
                               logger.info(f"[Stats] Loaded {len(alphas)} alphas from hopeful_alphas.json")
                           else:
                               logger.warning(f"[Stats] hopeful_alphas.json did not contain a list. Found type: {type(alphas_data)}")
            except (IOError, json.JSONDecodeError) as e:
                logger.error(f"[Stats] Error processing {HOPEFUL_ALPHAS_FILE}: {e}")
            except Exception as e:
                logger.error(f"[Stats] Unexpected error reading {HOPEFUL_ALPHAS_FILE}: {e}", exc_info=True)


    if alphas:
        try:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list)

            all_fitness = [a.get('performance', {}).get('fitness', 0) for a in valid_alphas_list]
            all_sharpe = [a.get('performance', {}).get('sharpe', 0) for a in valid_alphas_list]

            if all_fitness:
                stats['max_fitness'] = max(all_fitness) if all_fitness else 0.0 
                stats['avg_fitness'] = sum(all_fitness) / len(all_fitness) if all_fitness else 0.0
            if all_sharpe:
                stats['max_sharpe'] = max(all_sharpe) if all_sharpe else 0.0 

            def calculate_combined_score(report):
                if not isinstance(report, dict): return -float('inf')
                perf = report.get('performance', {})
                fitness = perf.get('fitness', -999)
                sharpe = perf.get('sharpe', 0.0)
                turnover = perf.get('turnover', 1.0)
                checks_summary = report.get('checks_summary', '0 PASS')
                passed_count = 0
                try:
                    match = re.match(r'(\d+)', checks_summary or '')
                    if match:
                        passed_count = int(match.group(1))
                except (ValueError, IndexError, TypeError): pass
                try:
                    sharpe = float(sharpe) if sharpe is not None else 0.0
                    turnover = float(turnover) if turnover is not None else 1.0
                except (ValueError, TypeError): sharpe, turnover = 0.0, 1.0
                return fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)

            # --- v5.4: 排序逻辑修改 ---
            # 1. 先创建一个包含所有计算字段的临时列表
            processed_alphas_temp = []
            processed_count = 0
            
            for alpha in valid_alphas_list:
                try: 
                    expression = alpha.get('expression')
                    if not expression: # 跳过没有表达式的无效条目
                        continue
                        
                    perf_data = alpha.get('performance', {})
                    fitness_val = perf_data.get('fitness', 0)
                    
                    summary = alpha.get('checks_summary', '')
                    summary_str = summary if summary is not None else ''

                    fail_match = fail_pattern.search(summary_str)
                    has_fail = bool(fail_match and int(fail_match.group(1)) > 0)
                    pending_match = pending_pattern.search(summary_str)
                    has_pending = bool(pending_match and int(pending_match.group(1)) > 0)
                    pass_match = pass_pattern.search(summary_str)
                    passed_count = int(pass_match.group(1)) if pass_match else 0

                    is_all_pass = passed_count >= 7 and not has_fail and not has_pending
                    is_submittable = passed_count >= 7 and not has_fail
                    is_submitted = bool(expression in submitted_set)

                    if is_submittable and not is_submitted:
                        stats['submittable_pending_count'] += 1

                    processed_alphas_temp.append({
                        "expression": expression,
                        "fitness": fitness_val,
                        "sharpe": perf_data.get('sharpe', 0),
                        "checks": summary_str,
                        "timestamp": alpha.get('timestamp', 'N/A'),
                        "is_all_pass": is_all_pass,
                        "is_submittable": is_submittable,
                        "is_submitted": is_submitted,
                        # 计算并存储用于第三级排序的综合分
                        "combined_score": calculate_combined_score(alpha) 
                    })
                    processed_count += 1
                except Exception as e:
                    logger.error(f"[Stats Process] Error processing alpha: {alpha.get('expression', 'N/A')}. Error: {e}", exc_info=True)

            logger.info(f"[Stats] Processed {processed_count}/{len(valid_alphas_list)} valid alphas for stats.")

            # 2. 定义你的三级排序 key
            def sort_key(alpha):
                # 1. (fitness >= 1) -> True (1) or False (0). (True 优先)
                sort_fitness = (alpha['fitness'] >= 1) 
                
                # 2. (not is_submitted) -> "Not Submitted" (True, 1) or "Submitted" (False, 0). (True 优先)
                sort_submitted = (not alpha['is_submitted'])
                
                # 3. combined_score -> (Higher score 优先)
                sort_combined = alpha['combined_score']
                
                # 我们将按此元组的顺序降序排序
                return (sort_fitness, sort_submitted, sort_combined)
                
            # 3. 执行排序 (reverse=True 意味着降序, True(1) > False(0))
            processed_alphas_temp.sort(key=sort_key, reverse=True)
            
            # 4. 将排序后的列表赋给 stats
            stats['all_alphas'] = processed_alphas_temp
            # --- v5.4 排序逻辑结束 ---

        except Exception as e:
             logger.error(f"[Stats] Unexpected error processing alphas list: {e}", exc_info=True)

    return stats
# --- v5.4 结束 ---

# --- v5.3: 合并 get_file_versions ---
def get_file_versions():
    versions = {}
    for name, filepath in FILES_TO_TRACK.items():
        try:
            if os.path.exists(filepath):
                mtime = os.stat(filepath).st_mtime
                versions[name] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            else:
                versions[name] = "文件未找到"
        except Exception as e:
            versions[name] = f"获取失败: {e}"
            logger.error(f"Error getting version for {name} ({filepath}): {e}")
    return versions
# --- v5.3 结束 ---

@app.route('/')
def dashboard():
    return render_template('dashboard_v4.html')

# --- v5.3: 合并 /status 路由 ---
@app.route('/status')
def status():
    logger.info("[API /status] Request received.") 
    try:
        data = {
            "miner": get_service_status('miner.log'),
            "evolver": get_service_status('evolver.log'),
            "hopeful_alphas": get_hopeful_alphas_stats(),
            "file_versions": get_file_versions()
        }
        
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate' 
        response.headers['Pragma'] = 'no-cache' 
        response.headers['Expires'] = '0' 
        
        logger.info("[API /status] Request completed successfully with no-cache headers.") 
        return response
        
    except Exception as e:
        logger.critical(f"[API /status] CRITICAL Error: {e}", exc_info=True) 
        return jsonify({"error": "Failed to retrieve status data due to an internal server error."}), 500
# --- v5.3 结束 ---

@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files:
        logger.warning(f"[API /download_logs] Invalid log file requested: {log_filename}")
        return "Invalid log file requested", 404
    try:
        logger.info(f"[API /download_logs] Serving file: {log_filename}")
        return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError:
        logger.error(f"[API /download_logs] File not found: {log_filename}")
        return f"Log file '{log_filename}' not found in '{LOG_DIR}/' directory.", 404
    except Exception as e:
        logger.error(f"[API /download_logs] Error downloading log {log_filename}: {e}", exc_info=True)
        return "Error downloading file", 500


# --- v5.2-debug: 保留加强日志: API 端点 ---
@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    operation = "Mark"
    logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json:
        logger.warning(f"[API /{operation.lower()}_submitted] Request is not JSON.")
        return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str):
        logger.warning(f"[API /{operation.lower()}_submitted] Invalid or missing expression.")
        return jsonify(status='error', message='Invalid or missing expression'), 400

    logger.info(f"[API /{operation.lower()}_submitted] Received expression (first 50 chars): {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas() 
        original_size = len(submitted_set)
        submitted_set.add(expression)
        new_size = len(submitted_set)

        if new_size > original_size:
            logger.info(f"[API /{operation.lower()}_submitted] Expression added to set. Attempting save.")
        else:
            logger.info(f"[API /{operation.lower()}_submitted] Expression already in set. Attempting save (idempotent).")

        if save_submitted_alphas(submitted_set): 
            logger.info(f"[API /{operation.lower()}_submitted] Operation successful for expression: {expression[:50]}...")
            return jsonify(status='success', message=operation+'ed') 
        else:
            logger.error(f"[API /{operation.lower()}_submitted] Save operation failed for expression: {expression[:50]}...")
            return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e:
        logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL Error: {e}", exc_info=True)
        return jsonify(status='error', message=f'Internal server error while {operation.lower()}ing'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    operation = "Unmark" 
    logger.info(f"[API /{operation.lower()}_submitted] Request received.")
    if not request.is_json:
        logger.warning(f"[API /{operation.lower()}_submitted] Request is not JSON.")
        return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str):
        logger.warning(f"[API /{operation.lower()}_submitted] Invalid or missing expression.")
        return jsonify(status='error', message='Invalid or missing expression'), 400

    logger.info(f"[API /{operation.lower()}_submitted] Received expression (first 50 chars): {expression[:50]}...")
    try:
        submitted_set = load_submitted_alphas() 
        original_size = len(submitted_set)
        submitted_set.discard(expression) 
        new_size = len(submitted_set)

        if new_size < original_size:
            logger.info(f"[API /{operation.lower()}_submitted] Expression removed from set. Attempting save.")
        else:
            logger.info(f"[API /{operation.lower()}_submitted] Expression was not in set. Attempting save (idempotent).")


        if save_submitted_alphas(submitted_set): 
            logger.info(f"[API /{operation.lower()}_submitted] Operation successful for expression: {expression[:50]}...")
            return jsonify(status='success', message=operation+'ed') 
        else:
            logger.error(f"[API /{operation.lower()}_submitted] Save operation failed for expression: {expression[:50]}...")
            return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e:
        logger.critical(f"[API /{operation.lower()}_submitted] CRITICAL Error: {e}", exc_info=True)
        return jsonify(status='error', message=f'Internal server error while {operation.lower()}ing'), 500
# --- 日志加强结束 ---


if __name__ == '__main__':
    if not os.path.exists(LOG_DIR):
        try:
            os.makedirs(LOG_DIR)
            logger.info(f"Created log directory: {LOG_DIR}") 
        except OSError as e:
            logger.error(f"Error creating log directory {LOG_DIR}: {e}") 

    try:
        os.stat_cache.clear()
        logger.info("Cleared os.stat_cache() on startup.")
    except AttributeError:
        logger.info("os.stat_cache() not available on this platform, skipping.")

    logger.info("Starting Flask application...") 
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)