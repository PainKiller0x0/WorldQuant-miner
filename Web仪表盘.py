# --- Web仪表盘.py v15.0 (Database Edition) ---
from flask import Flask, render_template, jsonify, send_from_directory, request, make_response
import json
import os
import re
import logging
import pandas as pd
import numpy as np
import time 
from datetime import datetime, timezone, timedelta

import utils
import database
from database import Alpha, get_db
from sqlalchemy import func

CURRENT_DASHBOARD_VERSION = "v15.0 (SQLite Database)"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
GENERATOR_FILE_PATH = os.path.join(BASE_DIR, "alpha_generator_ollama.py")

# --- 核心数据接口 (重写为 SQL 查询) ---

def get_hopeful_alphas_stats():
    stats = { "count": 0, "max_fitness": 0.0, "max_sharpe": 0.0, "avg_fitness": 0.0,
              "submittable_pending_count": 0, "successfully_submitted_count": 0,
              "total_submitted_count": 0, "all_alphas": [] }
    try:
        with get_db() as db:
            # 1. 基础统计
            total_count = db.query(func.count(Alpha.id)).scalar()
            stats['count'] = total_count
            
            # 2. 聚合指标 (只统计 Fitness > 0 的)
            metrics = db.query(
                func.max(Alpha.fitness),
                func.avg(Alpha.fitness),
                func.max(Alpha.sharpe)
            ).filter(Alpha.fitness > -900).first()
            
            stats['max_fitness'] = metrics[0] or 0.0
            stats['avg_fitness'] = metrics[1] or 0.0
            stats['max_sharpe'] = metrics[2] or 0.0
            
            # 3. 状态计数
            stats['submittable_pending_count'] = db.query(func.count(Alpha.id)).filter(
                Alpha.pass_count >= 7, 
                Alpha.fail_count == 0, 
                Alpha.is_submitted == False, 
                Alpha.is_failed_on_wq == False
            ).scalar()
            
            stats['successfully_submitted_count'] = db.query(func.count(Alpha.id)).filter(Alpha.is_submitted == True).scalar()
            stats['total_submitted_count'] = stats['successfully_submitted_count'] + db.query(func.count(Alpha.id)).filter(Alpha.is_failed_on_wq == True).scalar()

            # 4. 获取所有列表 (用于前端表格)
            # 限制返回 1000 条以保护前端，或者前端需要分页 (v15.0 暂全量返回但只查必要字段)
            all_alphas = db.query(Alpha).all()
            
            processed_list = []
            for a in all_alphas:
                # 重新计算 Dashboard Score (数据库已有部分逻辑，这里保持一致)
                score = a.calculate_score()
                
                # 判断状态
                is_submittable = (a.pass_count >= 7 and a.fail_count == 0)
                is_successfully_submitted = (a.is_submitted)
                
                processed_list.append({
                    "expression": a.expression,
                    "timestamp": a.created_at.isoformat() if a.created_at else "N/A",
                    "manual_timestamp": a.submitted_timestamp.isoformat() if a.submitted_timestamp else "N/A",
                    "checks_summary": a.checks_summary,
                    "is_submittable": is_submittable,
                    "is_submitted": a.is_submitted,
                    "is_failed_on_wq": a.is_failed_on_wq,
                    "is_successfully_submitted": is_successfully_submitted,
                    "dashboard_score": score,
                    "performance": {
                        "fitness": a.fitness,
                        "sharpe": a.sharpe,
                        "returns": a.returns,
                        "turnover": a.turnover
                    }
                })
            
            stats['all_alphas'] = processed_list

    except Exception as e:
        logger.error(f"[Stats] DB Error: {e}", exc_info=True)
        stats['error'] = str(e)
        
    return stats

def get_daily_submission_stats():
    stats = { "timestamps": [], "submittable_count": [], "submitted_count": [], "failed_count": [] }
    try:
        with get_db() as db:
            # 使用 SQL 进行按天聚合
            # SQLite 的日期截断函数是 strftime('%Y-%m-%d', column)
            
            # 1. Submitted
            submitted = db.query(
                func.strftime('%Y-%m-%d', Alpha.submitted_timestamp),
                func.count(Alpha.id)
            ).filter(Alpha.is_submitted == True).group_by(func.strftime('%Y-%m-%d', Alpha.submitted_timestamp)).all()
            
            # 2. Submittable (Created date)
            submittable = db.query(
                func.strftime('%Y-%m-%d', Alpha.created_at),
                func.count(Alpha.id)
            ).filter(Alpha.pass_count >= 7, Alpha.fail_count == 0).group_by(func.strftime('%Y-%m-%d', Alpha.created_at)).all()
            
            # 3. Failed (使用 created_at 近似，因为 failed_timestamp 可能没存单独字段，或者用 submitted_timestamp)
            # 注：database.py 里没专门的 failed_timestamp，通常复用 updated_at 或 submitted_timestamp
            # v15.0 暂用 created_at 统计 Failed 产生日
            failed = db.query(
                func.strftime('%Y-%m-%d', Alpha.created_at),
                func.count(Alpha.id)
            ).filter(Alpha.is_failed_on_wq == True).group_by(func.strftime('%Y-%m-%d', Alpha.created_at)).all()
            
            # 整理数据
            data_map = {}
            def add_to_map(rows, key):
                for date_str, count in rows:
                    if not date_str: continue
                    if date_str not in data_map: data_map[date_str] = {"sub":0, "ok":0, "fail":0}
                    data_map[date_str][key] = count

            add_to_map(submittable, "sub")
            add_to_map(submitted, "ok")
            add_to_map(failed, "fail")
            
            sorted_dates = sorted(data_map.keys())
            stats['timestamps'] = sorted_dates
            stats['submittable_count'] = [data_map[d]["sub"] for d in sorted_dates]
            stats['submitted_count'] = [data_map[d]["ok"] for d in sorted_dates]
            stats['failed_count'] = [data_map[d]["fail"] for d in sorted_dates]
            
    except Exception as e:
        logger.error(f"[Daily Stats] DB Error: {e}")
    return stats

def get_service_status(log_file):
    status = "UNKNOWN"; last_seen = "Never"; logs = "Log file not found."
    log_path = os.path.join(LOG_DIR, log_file)
    if os.path.exists(log_path):
        try:
            last_modified_time = datetime.fromtimestamp(os.path.getmtime(log_path))
            last_seen = last_modified_time.strftime('%Y-%m-%d %H:%M:%S')
            if datetime.now() - last_modified_time < timedelta(minutes=10): status = "RUNNING"
            else: status = "STALLED"
            from collections import deque
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: latest_lines = deque(f, maxlen=50)
            logs = "".join(reversed(latest_lines))
        except Exception as e: logs = f"Error: {e}"; status = "ERROR"
    else: status = "NOT FOUND"
    return {"status": status, "last_seen": last_seen, "logs": logs}

def get_version_from_file(file_path, version_regex_str):
    try:
        if not os.path.isfile(file_path): return "file_not_found"
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        match = re.compile(version_regex_str).search(content)
        return match.group(1) if match else "unknown"
    except: return "read_error"

# --- 路由定义 ---

@app.route('/')
def dashboard():
    return render_template('dashboard_v4.html')

@app.route('/settings')
def settings_page(): return render_template('settings.html')

@app.route('/chart')
def chart_page(): return render_template('chart.html')

@app.route('/pending')
def pending_page(): return render_template('pending.html')

@app.route('/status')
def status():
    try:
        config = utils.load_system_config()
        
        # 构建响应
        data = {
            "miner": get_service_status('miner.log'),
            "evolver": get_service_status('evolver.log'),
            "hopeful_alphas": get_hopeful_alphas_stats(),
        }
        
        # 看门狗状态
        llm_budgets = config.get("llm_budgets", {})
        wq_limiter = config.get("wq_api_limiter", {})
        
        miner_b = llm_budgets.get("miner", {})
        evolver_b = llm_budgets.get("evolver", {})
        
        now = time.time()
        last_fail = wq_limiter.get("last_failure_timestamp", 0)
        cd_time = wq_limiter.get("wq_429_cooldown_seconds", 60)
        cd_rem = max(0, round(cd_time - (now - last_fail))) if now - last_fail < cd_time else 0
        
        data["watchdog_status"] = {
            "llm_budget_used": miner_b.get("used_today", 0), # 兼容
            "llm_budget_limit": miner_b.get("daily_limit", 0),
            "miner_budget_used": miner_b.get("used_today", 0),
            "miner_budget_limit": miner_b.get("daily_limit", 0),
            "evolver_budget_used": evolver_b.get("used_today", 0),
            "evolver_budget_limit": evolver_b.get("daily_limit", 0),
            "wq_current_tpm_limit": wq_limiter.get("current_tpm_limit", "N/A"),
            "wq_cooldown_status": f"IN_COOLDOWN ({cd_rem}s)" if cd_rem > 0 else "OK",
            "wq_cooldown_remaining_sec": cd_rem
        }
        
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'no-cache, no-store'
        return response
    except Exception as e:
        logger.error(f"Status Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/version_info')
def version_info():
    g_ver = get_version_from_file(GENERATOR_FILE_PATH, r'CURRENT_GENERATOR_VERSION\s*=\s*["\'](v[0-9]+\.[0-9]+\.[^"\']*)["\']')
    return jsonify({"dashboard_version": CURRENT_DASHBOARD_VERSION, "generator_version": g_ver})

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    return jsonify(utils.load_system_config())

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    if not request.is_json: return jsonify(status='error', message='JSON required'), 400
    try:
        # 复用 utils v15.0 的配置保存逻辑 (依然写文件)
        # 逻辑与之前相同，从略，直接保存传入数据
        # 这里为了简化代码，假设前端传来的数据结构已经适配 v14.2 的格式
        current = utils.load_system_config()
        new_data = request.json
        
        # 简单合并逻辑 (生产环境建议做更细致的校验)
        def update_recursive(d, u):
            for k, v in u.items():
                if isinstance(v, dict):
                    d[k] = update_recursive(d.get(k, {}), v)
                else:
                    d[k] = v
            return d
            
        update_recursive(current, new_data)
        utils.save_system_config(current)
        return jsonify(status='success', message='配置已保存')
    except Exception as e:
        return jsonify(status='error', message=str(e)), 500

# --- 操作接口 (写库) ---

@app.route('/api/mark_submitted', methods=['POST'])
def mark_submitted():
    expr = request.json.get('expression')
    if database.mark_alpha_submitted(expr):
        return jsonify(status='success')
    return jsonify(status='error', message='Alpha not found'), 404

@app.route('/api/mark_failed_on_wq', methods=['POST'])
def mark_failed():
    data = request.json
    expr = data.get('expression')
    reason = data.get('reason', 'UNKNOWN')
    if database.mark_alpha_failed(expr, reason):
        return jsonify(status='success')
    return jsonify(status='error', message='Alpha not found'), 404

@app.route('/api/get_pending_alphas')
def get_pending_alphas():
    # 从 DB 获取并过滤
    # 也可以直接写 SQL: filter(pass_count>=7, is_submitted=False, is_failed=False)
    try:
        with get_db() as db:
            pendings = db.query(Alpha).filter(
                Alpha.pass_count >= 7,
                Alpha.fail_count == 0,
                Alpha.is_submitted == False,
                Alpha.is_failed_on_wq == False
            ).all()
            
            result = []
            for a in pendings:
                result.append({
                    "expression": a.expression,
                    "fitness": a.fitness,
                    "sharpe": a.sharpe,
                    "returns": a.returns,
                    "turnover": a.turnover,
                    "checks_summary": a.checks_summary,
                    "dashboard_score": a.calculate_score(),
                    "timestamp": a.created_at.isoformat() if a.created_at else "N/A"
                })
            
            # 按分排序
            result.sort(key=lambda x: x['dashboard_score'], reverse=True)
            return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 剩下的 chart 接口 (timeseries) 可复用 get_daily_submission_stats 逻辑，这里暂略
# v15.0 重点是保证核心流程跑通

@app.route('/download_logs/<log_filename>')
def download_logs(log_filename):
    return send_from_directory(LOG_DIR, log_filename, as_attachment=True)
# === 在这里插入缺失的路由 ===

@app.route('/api/v1/stats/submission_daily')
def api_stats_submission_daily():
    try:
        # 直接调用已定义的统计函数
        stats = get_daily_submission_stats()
        return jsonify(stats)
    except Exception as e:
        logger.error(f"[API Daily] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# =========================
@app.route('/api/v1/stats/timeseries')
def api_stats_timeseries():
    # 简化的时间序列实现，直接查库
    try:
        with get_db() as db:
            # 按小时聚合 created_at
            # SQLite: strftime('%Y-%m-%d %H:00', created_at)
            rows = db.query(
                func.strftime('%m-%d %H:00', Alpha.created_at),
                func.count(Alpha.id),
                func.avg(Alpha.fitness)
            ).group_by(func.strftime('%m-%d %H:00', Alpha.created_at)).all()
            
            timestamps = []
            counts = []
            fitness = []
            for ts, cnt, fit in rows:
                timestamps.append(ts)
                counts.append(cnt)
                fitness.append(round(fit, 4) if fit else 0)
                
            return jsonify({
                "timestamps": timestamps,
                "count": counts,
                "mean_fitness": fitness,
                "high_quality_count": counts # 暂且用总数代替
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    database.init_db() # 确保表存在
    app.run(host='0.0.0.0', port=8080, threaded=True)