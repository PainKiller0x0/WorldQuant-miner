from flask import Flask, render_template, jsonify, send_from_directory
import json
import os
from datetime import datetime
import requests
import logging
from requests.auth import HTTPBasicAuth

app = Flask(__name__)

# 配置日志，但只记录错误，避免刷屏
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

class AlphaDashboard:
    def __init__(self):
        self.hopeful_alphas_file = "hopeful_alphas.json"
        self.log_file = "logs/alpha_generator.log"
        self.credentials_file = "credential.txt"
        self.api_config_file = "api_config.json"

    def get_system_status(self) -> dict:
        """获取系统整体状态。"""
        return {
            "timestamp": datetime.now().isoformat(),
            "api_service": self.get_api_service_status(),
            "orchestrator": self.get_orchestrator_status(),
            "worldquant": self.get_worldquant_status(),
            "statistics": self.get_statistics(),
        }

    def get_api_service_status(self) -> dict:
        """检查外部API代理服务的状态。"""
        if not os.path.exists(self.api_config_file):
            return {"status": "error", "message": "api_config.json not found"}
        try:
            with open(self.api_config_file, 'r') as f:
                config = json.load(f)
            url = config.get('base_url', '')
            # 尝试访问一个通常存在的端点，例如OpenAI兼容的/v1/models
            response = requests.get(f"{url.rsplit('/v1', 1)[0]}/v1/models", timeout=5)
            if response.status_code == 200:
                return {"status": "connected", "message": "Proxy Connected"}
            else:
                 return {"status": "not_responding", "message": f"Proxy Status: {response.status_code}"}
        except Exception:
            return {"status": "error", "message": "Proxy Unreachable"}

    def get_orchestrator_status(self) -> dict:
        """从日志文件中推断Orchestrator的状态。"""
        if not os.path.exists(self.log_file):
            return {"status": "unknown", "last_activity": "Log file not found."}
        try:
            with open(self.log_file, "r", encoding='utf-8') as f:
                lines = f.readlines()
            if not lines:
                 return {"status": "idle", "last_activity": "Log file is empty."}
            last_line = lines[-1].strip()
            status = "active"
            if "等待" in last_line or "本轮结束" in last_line:
                status = "idle"
            elif "失败" in last_line or "Error" in last_line or "失败" in last_line:
                status = "error"
            return {"status": status, "last_activity": last_line}
        except Exception:
            return {"status": "unknown", "last_activity": "Could not read log file."}

    def get_worldquant_status(self) -> dict:
        """检查 WorldQuant Brain API 状态。"""
        if not os.path.exists(self.credentials_file):
            return {"status": "error", "message": "credential.txt not found"}
        try:
            with open(self.credentials_file, 'r') as f:
                user_id, api_key = json.load(f)
            session = requests.Session()
            session.auth = HTTPBasicAuth(user_id, api_key)
            response = session.post("https://api.worldquantbrain.com/authentication", timeout=10)
            if response.status_code == 201:
                return {"status": "connected", "message": "Authentication successful"}
            else:
                return {"status": "auth_failed", "message": f"Auth Failed: {response.status_code}"}
        except Exception:
            return {"status": "unknown", "message": "Connection Check Failed"}

    def get_statistics(self) -> dict:
        """从 hopeful_alphas.json 获取统计信息。"""
        stats = { "hopeful_alphas_count": 0, "highest_fitness": "N/A", "avg_sharpe": "N/A" }
        if not os.path.exists(self.hopeful_alphas_file):
            return stats
        try:
            with open(self.hopeful_alphas_file, "r", encoding='utf-8') as f:
                content = f.read()
                if not content.strip(): return stats
                alphas = json.loads(content)
            
            if not alphas: return stats
            stats["hopeful_alphas_count"] = len(alphas)
            
            fitness_scores = [a.get("performance", {}).get("fitness", 0) for a in alphas if a.get("performance", {}).get("fitness") is not None]
            if fitness_scores:
                stats["highest_fitness"] = round(max(fitness_scores), 2)

            sharpe_ratios = [a.get("performance", {}).get("sharpe", 0) for a in alphas if a.get("performance", {}).get("sharpe") is not None]
            if sharpe_ratios:
                stats["avg_sharpe"] = round(sum(sharpe_ratios) / len(sharpe_ratios), 2)
        except (IOError, json.JSONDecodeError):
            pass
        return stats

    def get_logs(self, lines: int = 200) -> list:
        """直接从日志文件读取日志。"""
        if not os.path.exists(self.log_file):
            return ["Log file not found at: " + self.log_file]
        try:
            with open(self.log_file, "r", encoding='utf-8') as f:
                all_lines = f.readlines()
            return [l.strip() for l in all_lines[-lines:]]
        except Exception as e:
            return [f"Error reading log file: {e}"]

dashboard = AlphaDashboard()

@app.route("/")
def index():
    return render_template("dashboard_v3.html")

@app.route("/api/status")
def api_status():
    return jsonify(dashboard.get_system_status())

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": dashboard.get_logs()})

if __name__ == "__main__":
    if not os.path.exists("templates/dashboard_v3.html"):
        print("ERROR: templates/dashboard_v3.html not found!")
        print("Please create it with the provided HTML content.")
    else:
        print("Starting Alpha Miner Dashboard v3...")
        print("Access at: http://localhost:5001")
        app.run(host="0.0.0.0", port=5000)