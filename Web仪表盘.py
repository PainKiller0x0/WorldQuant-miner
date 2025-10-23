from flask import Flask, render_template, jsonify, send_from_directory, request
import json
import os
import re
import threading
from datetime import datetime, timedelta
from collections import deque # <-- BUG 2 修复: 导入 deque

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
HOPEFUL_ALPHAS_FILE = os.path.join(BASE_DIR, 'hopeful_alphas.json')
SUBMITTED_ALPHAS_FILE = os.path.join(BASE_DIR, 'submitted_alphas.json')

HEARTBEAT_TIMEOUT = timedelta(minutes=10)
file_lock = threading.Lock()

def load_submitted_alphas():
    with file_lock:
        if not os.path.exists(SUBMITTED_ALPHAS_FILE):
            return set()
        try:
            with open(SUBMITTED_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, (list, set)):
                    return set(data)
                else:
                    print(f"Warning: submitted_alphas.json did not contain a list/set.")
                    return set()
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading submitted alphas file: {e}")
            return set()

def save_submitted_alphas(submitted_set):
    with file_lock:
        try:
            with open(SUBMITTED_ALPHAS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(submitted_set), f, indent=4)
        except IOError as e:
            print(f"Error saving submitted alphas file: {e}")

# --- BUG 2 修复: 重写此函数以避免内存溢出 ---
def get_service_status(log_file):
    status = "UNKNOWN"
    last_seen = "Never"
    logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    
    if os.path.exists(log_path):
        try:
            # 1. 获取最后修改时间来判断状态 (这部分逻辑不变)
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            
            if datetime.now() - last_modified_time < HEARTBEAT_TIMEOUT:
                status = "RUNNING"
            else:
                status = "STALLED"

            # 2. **BUG 2 核心修复**
            # 不使用 f.readlines()，因为它会读取整个文件。
            # 使用 deque 高效地流式读取文件，并只在内存中保留最后 50 行。
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                latest_lines_deque = deque(f, maxlen=50)
            
            # Deque 自动保留了最后50行
            latest_lines = list(latest_lines_deque)
            # 反转顺序，让最新的日志显示在最上面 (符合原版逻辑)
            latest_lines.reverse() 
            logs = "".join(latest_lines)

        except Exception as e:
            logs = f"Error reading log file: {e}"
            status = "ERROR" # 如果读取日志失败，也标记为错误
            print(f"Error getting service status for {log_file}: {e}")
    else:
        status = "NOT FOUND" # 日志文件不存在
        
    return {"status": status, "last_seen": last_seen, "logs": logs}
# --- 修复结束 ---

def get_hopeful_alphas_stats():
    stats = {
        "count": 0,
        "max_fitness": 0.0,
        "max_sharpe": 0.0,
        "avg_fitness": 0.0,
        "all_alphas": []
    }
    submitted_set = load_submitted_alphas()
    pass_pattern = re.compile(r'(\d+)\s+PASS')
    alphas = [] # 初始化为空列表

    if os.path.exists(HOPEFUL_ALPHAS_FILE):
        try:
            with file_lock:
                if os.path.getsize(HOPEFUL_ALPHAS_FILE) > 0:
                    with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content:
                           alphas = json.loads(content)
                        if not isinstance(alphas, list):
                            print(f"Warning: hopeful_alphas.json did not contain a list.")
                            alphas = []
                else:
                    print("Warning: hopeful_alphas.json is empty.")
                    alphas = []
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error processing hopeful_alphas.json: {e}")
            alphas = []

    if alphas:
        try:
            stats['count'] = len(alphas)

            all_fitness = [a.get('performance', {}).get('fitness', 0) for a in alphas if isinstance(a, dict)]
            all_sharpe = [a.get('performance', {}).get('sharpe', 0) for a in alphas if isinstance(a, dict)]

            if all_fitness:
                stats['max_fitness'] = max(all_fitness)
                stats['avg_fitness'] = sum(all_fitness) / len(all_fitness)
            if all_sharpe:
                stats['max_sharpe'] = max(all_sharpe)

            def calculate_combined_score(report):
                if not isinstance(report, dict): return -float('inf')
                fitness = report.get('performance', {}).get('fitness', -999)
                sharpe = report.get('performance', {}).get('sharpe', 0.0)
                turnover = report.get('performance', {}).get('turnover', 1.0)
                checks_summary = report.get('checks_summary', '0 PASS')
                passed_count = 0
                try:
                    match = re.match(r'(\d+)', checks_summary)
                    if match:
                        passed_count = int(match.group(1))
                except (ValueError, IndexError, TypeError):
                    passed_count = 0
                try:
                    sharpe = float(sharpe)
                    turnover = float(turnover) if turnover is not None else 1.0
                except (ValueError, TypeError):
                    sharpe = 0.0
                    turnover = 1.0

                score = fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)
                return score

            valid_alphas = [a for a in alphas if isinstance(a, dict)]
            valid_alphas.sort(key=calculate_combined_score, reverse=True)

            for alpha in valid_alphas:
                summary = alpha.get('checks_summary', '')
                expression = alpha.get('expression') 

                has_fail = "FAIL" in summary
                has_pending = "PENDING" in summary
                pass_match = pass_pattern.search(summary)
                passed_count = int(pass_match.group(1)) if pass_match else 0

                is_all_pass = passed_count >= 7 and not has_fail and not has_pending
                is_submittable = passed_count >= 7 and not has_fail and has_pending
                is_submitted = bool(expression and expression in submitted_set)

                stats['all_alphas'].append({
                    "expression": expression,
                    "fitness": alpha.get('performance', {}).get('fitness', 0),
                    "sharpe": alpha.get('performance', {}).get('sharpe', 0),
                    "checks": summary,
                    "timestamp": alpha.get('timestamp', 'N/A'),
                    "is_all_pass": is_all_pass,
                    "is_submittable": is_submittable,
                    "is_submitted": is_submitted
                })
        except Exception as e:
             print(f"Unexpected error processing alphas list: {e}")

    return stats

@app.route('/')
def dashboard():
    return render_template('dashboard_v4.html')

@app.route('/status')
def status():
    try:
        data = {
            "miner": get_service_status('miner.log'),
            "evolver": get_service_status('evolver.log'),
            "hopeful_alphas": get_hopeful_alphas_stats()
        }
        return jsonify(data)
    except Exception as e:
        print(f"Error in /status route: {e}")
        return jsonify({"error": "Failed to get status data", "details": str(e)}), 500


@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log', 'miner_issues.log', 'evolver_issues.log']
    if log_filename not in allowed_files:
        return "Invalid log file requested", 404
    try:
        return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError:
        return f"Log file {log_filename} not found in {LOG_DIR}/ directory.", 404

@app.route('/api/mark_submitted', methods=['POST'])
def mark_alpha_submitted():
    data = request.json
    expression = data.get('expression')
    if not expression:
        return jsonify(status='error', message='No expression provided'), 400
    try:
        submitted_set = load_submitted_alphas()
        submitted_set.add(expression)
        save_submitted_alphas(submitted_set)
        return jsonify(status='success', message=f'Marked: {expression[:20]}...')
    except Exception as e:
        print(f"Error in mark_submitted: {e}")
        return jsonify(status='error', message=f'Server error: {e}'), 500


@app.route('/api/unmark_submitted', methods=['POST'])
def unmark_alpha_submitted():
    data = request.json
    expression = data.get('expression')
    if not expression:
        return jsonify(status='error', message='No expression provided'), 400
    try:
        submitted_set = load_submitted_alphas()
        submitted_set.discard(expression)
        save_submitted_alphas(submitted_set)
        return jsonify(status='success', message=f'Unmarked: {expression[:20]}...')
    except Exception as e:
        print(f"Error in unmark_submitted: {e}")
        return jsonify(status='error', message=f'Server error: {e}'), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)