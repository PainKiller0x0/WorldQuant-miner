from flask import Flask, jsonify, render_template
import os
import json
from datetime import datetime

app = Flask(__name__)

LOGS_DIR = 'logs'
HOPEFUL_ALPHAS_FILE = 'hopeful_alphas.json'
TESTED_ALPHAS_LOG_FILE = 'tested_alphas_log.json'
MINER_LOG_FILE = os.path.join(LOGS_DIR, 'miner.log')
EVOLVER_LOG_FILE = os.path.join(LOGS_DIR, 'evolver.log')

def read_last_n_lines(file_path, n=50):
    """读取文件末尾N行，兼容文件不存在的情况"""
    if not os.path.exists(file_path):
        return f"日志文件不存在: {file_path}"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return ''.join(lines[-n:])
    except Exception as e:
        return f"读取日志时出错: {e}"

@app.route('/')
def index():
    return render_template('dashboard_v4.html')

@app.route('/api/data')
def api_data():
    # 1. 读取两个矿机的日志
    miner_log_content = read_last_n_lines(MINER_LOG_FILE, 100)
    evolver_log_content = read_last_n_lines(EVOLVER_LOG_FILE, 100)

    # 2. 读取高质量Alphas
    hopeful_alphas = []
    if os.path.exists(HOPEFUL_ALPHAS_FILE):
        try:
            with open(HOPEFUL_ALPHAS_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                if content:
                    hopeful_alphas = json.loads(content)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error reading hopeful alphas: {e}")

    # 3. 计算KPI
    total_hopeful = len(hopeful_alphas)
    best_fitness = 0
    if hopeful_alphas:
        best_fitness = max(a.get('performance', {}).get('fitness', 0) for a in hopeful_alphas)

    total_tested = 0
    if os.path.exists(TESTED_ALPHAS_LOG_FILE):
        try:
            with open(TESTED_ALPHAS_LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                if content:
                    total_tested = len(json.loads(content))
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error reading tested alphas log: {e}")

    kpis = {
        "total_hopeful": total_hopeful,
        "best_fitness": f"{best_fitness:.3f}",
        "total_tested": total_tested
    }
    
    return jsonify({
        'miner_log': miner_log_content,
        'evolver_log': evolver_log_content,
        'alphas': hopeful_alphas,
        'kpis': kpis,
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)