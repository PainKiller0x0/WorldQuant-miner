# Web仪表盘.py (v1.1 - 排序修正版)
from flask import Flask, render_template, jsonify, request
import json
import os
import time
from datetime import datetime
import requests
import logging
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


class AlphaDashboard:
    def __init__(self):
        self.hopeful_alphas_file = "hopeful_alphas.json"
        self.log_file = "logs/alpha_generator.log"
        self.credentials_file = "credential.txt"
        self.api_config_file = "api_config.json"

        self.wq_status_cache = {"status": "unknown", "message": "Initializing..."}
        self.wq_last_check_time = 0
        self.cache_ttl = 60

    def get_system_status(self) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "api_service": self.get_api_service_status(),
            "orchestrator": self.get_orchestrator_status(),
            "worldquant": self.get_worldquant_status(),
            "statistics": self.get_statistics(),
        }

    def get_api_service_status(self) -> dict:
        if not os.path.exists(self.api_config_file):
            return {"status": "error", "message": "api_config.json not found"}
        try:
            with open(self.api_config_file, 'r') as f:
                config = json.load(f)
            url = config.get('base_url', '')
            api_key = config.get('api_keys', [''])[0]
            headers = {"Authorization": f"Bearer {api_key}"}
            test_url = f"{url.rsplit('/v1', 1)[0]}/v1/models"
            response = requests.get(test_url, headers=headers, timeout=5)
            return {"status": "connected", "message": "Proxy Connected"} if response.status_code == 200 else {"status": "not_responding", "message": f"Proxy Status: {response.status_code}"}
        except Exception:
            return {"status": "error", "message": "Proxy Unreachable"}

    def get_orchestrator_status(self) -> dict:
        if not os.path.exists(self.log_file):
            return {"status": "unknown", "last_activity": "Log file not found."}
        try:
            with open(self.log_file, "r", encoding='utf-8') as f:
                lines = f.readlines()
            if not lines:
                return {"status": "idle", "last_activity": "Log file is empty."}
            last_line = lines[-1].strip()
            status = "active"
            if any(s in last_line for s in ["等待", "本轮结束"]):
                status = "idle"
            elif any(s in last_line for s in ["失败", "Error", "错误"]):
                status = "error"
            return {"status": status, "last_activity": last_line}
        except Exception as e:
            return {"status": "unknown", "last_activity": f"Could not read log file: {e}"}

    def _parse_credentials(self, content: str) -> tuple:
        try:
            data = json.loads(content)
            if isinstance(data, list) and len(data) == 2:
                return data[0], data[1]
        except json.JSONDecodeError:
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            creds = {}
            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    creds[key.strip()] = value.strip()
            user = creds.get('user_id') or creds.get('username')
            key = creds.get('api_key') or creds.get('password')
            if user and key:
                return user, key
        raise ValueError("Unknown credential format")

    def get_worldquant_status(self) -> dict:
        current_time = time.time()
        if (current_time - self.wq_last_check_time) < self.cache_ttl:
            return self.wq_status_cache

        self.wq_last_check_time = current_time
        if not os.path.exists(self.credentials_file):
            self.wq_status_cache = {"status": "error", "message": "credential.txt not found"}
            return self.wq_status_cache

        try:
            with open(self.credentials_file, 'r', encoding='utf-8') as f:
                content = f.read()
            user_id, api_key = self._parse_credentials(content)
            session = requests.Session()
            session.auth = HTTPBasicAuth(user_id, api_key)
            response = session.post("https://api.worldquantbrain.com/authentication", timeout=10)
            if response.status_code == 201:
                self.wq_status_cache = {"status": "connected", "message": "Authentication successful"}
            elif response.status_code == 429:
                self.wq_status_cache = {"status": "rate_limited", "message": "Rate Limited (429)"}
            else:
                self.wq_status_cache = {"status": "auth_failed", "message": f"Auth Failed: {response.status_code}"}
        except Exception as e:
            self.wq_status_cache = {"status": "error", "message": f"Check Failed: {str(e)}"}
        return self.wq_status_cache

    def get_statistics(self) -> dict:
        stats = {"hopeful_alphas_count": 0, "highest_fitness": "N/A", "avg_sharpe": "N/A"}
        if not os.path.exists(self.hopeful_alphas_file):
            return stats
        try:
            with open(self.hopeful_alphas_file, "r", encoding='utf-8') as f:
                content = f.read()
            if not content.strip():
                return stats
            alphas = json.loads(content)
            if not alphas:
                return stats
            stats["hopeful_alphas_count"] = len(alphas)
            fitness = [a.get("performance", {}).get("fitness") for a in alphas if a.get("performance", {}).get("fitness") is not None]
            sharpe = [a.get("performance", {}).get("sharpe") for a in alphas if a.get("performance", {}).get("sharpe") is not None]
            if fitness:
                stats["highest_fitness"] = round(max(fitness), 2)
            if sharpe:
                stats["avg_sharpe"] = round(sum(sharpe) / len(sharpe), 2)
        except (IOError, json.JSONDecodeError, TypeError):
            pass
        return stats

    def get_logs(self, lines: int = 200) -> list:
        if not os.path.exists(self.log_file):
            return ["Log file not found at: " + self.log_file]
        try:
            with open(self.log_file, "r", encoding='utf-8') as f:
                return [l.strip() for l in f.readlines()[-lines:]]
        except Exception as e:
            return [f"Error reading log file: {e}"]

    def get_recent_alphas(self, n: int = 10) -> list:
        """返回最近的 n 个 alpha（按时间戳倒序）"""
        if not os.path.exists(self.hopeful_alphas_file):
            return []
        try:
            with open(self.hopeful_alphas_file, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                return []
            alphas = json.loads(content)
            if not isinstance(alphas, list):
                return []

            # --- [核心修正] ---
            # 不再依赖文件顺序，而是显式地按时间戳排序
            alphas.sort(key=lambda x: x.get('timestamp', '1970-01-01 00:00:00'), reverse=True)
            
            # 取排序后的前 n 个，即最新的 n 个
            recent = alphas[:n]
            
            out = []
            for idx, a in enumerate(recent):
                perf = a.get("performance", {}) or {}
                out.append({
                    "name": a.get("alpha_id") or f"Alpha_{idx}",
                    "fitness": perf.get("fitness"),
                    "sharpe": perf.get("sharpe"),
                    "created_at": a.get("timestamp", "N/A"),
                    "formula": a.get("expression", "N/A"),
                    "result_url": a.get("result_url"),
                    "checks_summary": a.get("checks_summary", "") # 传递检查摘要
                })
            return out
        except Exception as e:
            app.logger.error(f"Error in get_recent_alphas: {e}")
            return [{"error": str(e)}]

dashboard = AlphaDashboard()

@app.route("/")
def index():
    return render_template("dashboard_v3.html")

@app.route("/api/status")
def api_status():
    return jsonify(dashboard.get_system_status())

@app.route("/api/logs")
def api_logs():
    lines = request.args.get("lines", 200, type=int)
    return jsonify({"logs": dashboard.get_logs(lines=lines)})

@app.route("/api/recent_alphas")
def api_recent_alphas():
    n = request.args.get("n", 10, type=int)
    return jsonify({"recent": dashboard.get_recent_alphas(n=n)})

if __name__ == "__main__":
    if not os.path.exists("templates/dashboard_v3.html"):
        print("ERROR: templates/dashboard_v3.html not found!")
    else:
        print("Starting Alpha Miner Dashboard v3.7...")
        print("Access at: http://localhost:5001")
        app.run(host="0.0.0.0", port=5000, debug=False)