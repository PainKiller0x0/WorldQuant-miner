from flask import Flask, render_template, jsonify, send_from_directory, request
import json
import os
import re
import threading
from datetime import datetime, timedelta
from collections import deque
import os.path

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.join(BASE_DIR, 'submitted_alphas.json')
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

FILES_TO_TRACK = {
    "仪表盘后端 (Py)": os.path.join(BASE_DIR, "Web仪表盘.py"),
    "仪表盘前端 (HTML)": os.path.join(TEMPLATE_DIR, "dashboard_v4.html"),
    "核心生成器 (Py)": os.path.join(BASE_DIR, "alpha_generator_ollama.py")
}

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
file_lock = threading.Lock()
hopeful_lock = threading.Lock()

def load_submitted_alphas():
    with file_lock:
        if not os.path.exists(SUBMITTED_ALPHAS_FILE):
            return set()
        try:
            if os.path.getsize(SUBMITTED_ALPHAS_FILE) > 0:
                with open(SUBMITTED_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, (list, set)):
                        return set(data)
                    else:
                        print(f"Warning: submitted_alphas.json did not contain a list/set. Found type: {type(data)}")
                        return set()
            else:
                return set()
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading {SUBMITTED_ALPHAS_FILE}: {e}")
            return set()

def save_submitted_alphas(submitted_set):
    with file_lock:
        try:
            with open(SUBMITTED_ALPHAS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(submitted_set), f, indent=4)
            return True
        except IOError as e:
            print(f"Error saving {SUBMITTED_ALPHAS_FILE}: {e}")
            return False

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
            print(f"Error getting service status for {log_file}: {e}")
    else:
        status = "NOT FOUND"

    return {"status": status, "last_seen": last_seen, "logs": logs}


def get_hopeful_alphas_stats():
    stats = {
        "count": 0,
        "max_fitness": 0.0,
        "max_sharpe": 0.0,
        "avg_fitness": 0.0,
        "all_alphas": []
    }
    submitted_set = load_submitted_alphas()
    # 编译正则表达式以提高效率
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
                           else:
                               print(f"Warning: hopeful_alphas.json did not contain a list. Found type: {type(alphas_data)}")
            except (IOError, json.JSONDecodeError) as e:
                print(f"Error processing {HOPEFUL_ALPHAS_FILE}: {e}")

    if alphas:
        try:
            valid_alphas_list = [a for a in alphas if isinstance(a, dict)]
            stats['count'] = len(valid_alphas_list) # 使用过滤后的列表计数

            all_fitness = [a.get('performance', {}).get('fitness', 0) for a in valid_alphas_list]
            all_sharpe = [a.get('performance', {}).get('sharpe', 0) for a in valid_alphas_list]

            if all_fitness:
                stats['max_fitness'] = max(all_fitness)
                stats['avg_fitness'] = sum(all_fitness) / len(all_fitness) if all_fitness else 0.0
            if all_sharpe:
                stats['max_sharpe'] = max(all_sharpe)

            def calculate_combined_score(report):
                # (此函数逻辑保持 v5.0 不变)
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
                except (ValueError, IndexError, TypeError):
                    passed_count = 0
                try:
                    sharpe = float(sharpe) if sharpe is not None else 0.0
                    turnover = float(turnover) if turnover is not None else 1.0
                except (ValueError, TypeError):
                    sharpe = 0.0
                    turnover = 1.0
                score = fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)
                return score

            valid_alphas_list.sort(key=calculate_combined_score, reverse=True)

            for alpha in valid_alphas_list:
                summary = alpha.get('checks_summary', '')
                expression = alpha.get('expression')
                summary_str = summary if summary is not None else ''

                # --- 徽章逻辑 Bug 修复 ---
                # 使用正则表达式更精确地判断是否有 FAIL
                fail_match = fail_pattern.search(summary_str)
                # 只有当匹配到 FAIL 且数量 > 0 时才算失败
                has_fail = bool(fail_match and int(fail_match.group(1)) > 0)

                # 使用正则表达式更精确地判断是否有 PENDING
                pending_match = pending_pattern.search(summary_str)
                # 只有当匹配到 PENDING 且数量 > 0 时才算有挂起
                has_pending = bool(pending_match and int(pending_match.group(1)) > 0)

                # 获取 PASS 数量
                pass_match = pass_pattern.search(summary_str)
                passed_count = int(pass_match.group(1)) if pass_match else 0

                # 完美策略: >= 7 PASS, 0 FAIL, 0 PENDING
                is_all_pass = passed_count >= 7 and not has_fail and not has_pending

                # 可提交策略: >= 7 PASS, 0 FAIL (允许 PENDING)
                is_submittable = passed_count >= 7 and not has_fail
                # --- 修复结束 ---


                is_submitted = bool(expression and expression in submitted_set)
                perf_data = alpha.get('performance', {})
                stats['all_alphas'].append({
                    "expression": expression,
                    "fitness": perf_data.get('fitness', 0),
                    "sharpe": perf_data.get('sharpe', 0),
                    "checks": summary_str,
                    "timestamp": alpha.get('timestamp', 'N/A'),
                    "is_all_pass": is_all_pass,
                    "is_submittable": is_submittable, # 现在应该能正确计算了
                    "is_submitted": is_submitted
                })
        except Exception as e:
             print(f"Unexpected error processing alphas list: {e}", exc_info=True)

    return stats


def get_file_versions():
    versions = {}
    for name, filepath in FILES_TO_TRACK.items():
        try:
            if os.path.exists(filepath):
                mtime = os.path.getmtime(filepath)
                versions[name] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            else:
                versions[name] = "文件未找到"
        except Exception as e:
            versions[name] = f"获取失败: {e}"
            print(f"Error getting version for {name} ({filepath}): {e}")
    return versions

@app.route('/')
def dashboard():
    return render_template('dashboard_v4.html')

@app.route('/status')
def status():
    try:
        data = {
            "miner": get_service_status('miner.log'),
            "evolver": get_service_status('evolver.log'),
            "hopeful_alphas": get_hopeful_alphas_stats(),
            "file_versions": get_file_versions()
        }
        return jsonify(data)
    except Exception as e:
        print(f"CRITICAL Error in /status route: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve status data due to an internal server error."}), 500


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files:
        return "Invalid log file requested", 404
    try:
        return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError:
        return f"Log file '{log_filename}' not found in '{LOG_DIR}/' directory.", 404
    except Exception as e:
        print(f"Error downloading log {log_filename}: {e}")
        return "Error downloading file", 500


@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    if not request.is_json:
        return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str):
        return jsonify(status='error', message='Invalid or missing expression'), 400

    try:
        submitted_set = load_submitted_alphas()
        submitted_set.add(expression)
        if save_submitted_alphas(submitted_set):
            return jsonify(status='success', message='Marked')
        else:
            return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e:
        print(f"CRITICAL Error in mark_submitted: {e}", exc_info=True)
        return jsonify(status='error', message=f'Internal server error while marking'), 500

@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    if not request.is_json:
        return jsonify(status='error', message='Request must be JSON'), 400
    data = request.json
    expression = data.get('expression')
    if not expression or not isinstance(expression, str):
        return jsonify(status='error', message='Invalid or missing expression'), 400

    try:
        submitted_set = load_submitted_alphas()
        submitted_set.discard(expression)
        if save_submitted_alphas(submitted_set):
            return jsonify(status='success', message='Unmarked')
        else:
            return jsonify(status='error', message='Failed to save submission status'), 500
    except Exception as e:
        print(f"CRITICAL Error in unmark_submitted: {e}", exc_info=True)
        return jsonify(status='error', message=f'Internal server error while unmarking'), 500

if __name__ == '__main__':
    if not os.path.exists(LOG_DIR):
        try:
            os.makedirs(LOG_DIR)
            print(f"Created log directory: {LOG_DIR}")
        except OSError as e:
            print(f"Error creating log directory {LOG_DIR}: {e}")

    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)

