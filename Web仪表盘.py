from flask import Flask, render_template, jsonify, send_from_directory
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)

LOG_DIR = 'logs'
HEARTBEAT_TIMEOUT = timedelta(minutes=10)

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

            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                logs = "".join(lines[-50:])
        except Exception as e:
            logs = f"Error reading log file: {e}"
    
    return {"status": status, "last_seen": last_seen, "logs": logs}

def get_hopeful_alphas_stats():
    stats = {
        "count": 0,
        "max_fitness": 0.0,
        "max_sharpe": 0.0,
        "avg_fitness": 0.0,
        "all_alphas": [] # v7.3 修改: 从 top_5 改为 all_alphas
    }
    
    hopeful_file = 'hopeful_alphas.json'
    if os.path.exists(hopeful_file):
        try:
            with open(hopeful_file, 'r', encoding='utf-8') as f:
                alphas = json.load(f)
            
            if alphas:
                stats['count'] = len(alphas)
                stats['max_fitness'] = max(a.get('performance', {}).get('fitness', 0) for a in alphas)
                stats['max_sharpe'] = max(a.get('performance', {}).get('sharpe', 0) for a in alphas)
                stats['avg_fitness'] = sum(a.get('performance', {}).get('fitness', 0) for a in alphas) / len(alphas)

                # 按综合评分排序 (与 alpha_generator_ollama.py v6.8+ 保持一致)
                def calculate_combined_score(report):
                    fitness = report.get('performance', {}).get('fitness', -999)
                    sharpe = report.get('performance', {}).get('sharpe', 0.0)
                    turnover = report.get('performance', {}).get('turnover', 1.0)
                    checks_summary = report.get('checks_summary', '0 PASS')
                    try: passed_count = int(checks_summary.split(' ')[0])
                    except (ValueError, IndexError): passed_count = 0
                    score = fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)
                    return score
                
                alphas.sort(key=calculate_combined_score, reverse=True)
                
                # v7.3 修改: 发送所有alpha到前端
                for alpha in alphas:
                    stats['all_alphas'].append({
                        "expression": alpha.get('expression'),
                        "fitness": alpha.get('performance', {}).get('fitness', 0),
                        "sharpe": alpha.get('performance', {}).get('sharpe', 0),
                        "checks": alpha.get('checks_summary', 'N/A'),
                        "timestamp": alpha.get('timestamp', 'N/A')
                    })
        except (IOError, json.JSONDecodeError, ZeroDivisionError) as e:
            print(f"Error processing hopeful_alphas.json: {e}")

    return stats

@app.route('/')
def dashboard():
    return render_template('dashboard_v4.html')

@app.route('/status')
def status():
    data = {
        "miner": get_service_status('miner.log'),
        "evolver": get_service_status('evolver.log'),
        "hopeful_alphas": get_hopeful_alphas_stats()
    }
    return jsonify(data)

@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    allowed_files = ['miner.log', 'evolver.log', 'archaeologist.log', 'cron.log']
    if log_filename not in allowed_files:
        return "Invalid log file requested", 404
    
    try:
        return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
    except FileNotFoundError:
        return f"Log file {log_filename} not found in {LOG_DIR}/ directory.", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)