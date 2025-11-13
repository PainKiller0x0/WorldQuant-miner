# --- llm_provider.py v17.0 (Active Node Tracking) ---
import logging
import json
import re
import random
from openai import OpenAI
from datetime import datetime, timezone, timedelta
import time
from utils import load_system_config, save_system_config

logger = logging.getLogger(__name__)

class LLMProvider:
    def __init__(self, api_config_path):
        self.api_config_path = api_config_path
        self.clients = {} 
        self.models = {}
        self.circuit_breaker = {} 
        self.CB_THRESHOLD = 2       
        self.CB_TIMEOUT = 600       
        self.backup_fleets = {'miner': [], 'evolver': []}
        self._load_config()

    def _load_config(self):
        try:
            with open(self.api_config_path, 'r') as f: config = json.load(f)
            
            def load_role(role_key, config_key):
                conf = config.get(config_key)
                if not conf: return
                self.models[role_key] = conf.get('model_name')
                self.clients[role_key] = OpenAI(api_key=conf.get('api_key'), base_url=conf.get('base_url'))
                self.circuit_breaker[role_key] = {'fails': 0, 'last_fail_time': 0}
                logger.info(f"Loaded [{role_key}]: {self.models[role_key]}")

            load_role('miner', 'miner_config')
            load_role('evolver', 'evolver_config')
            
            for role in ['miner', 'evolver']:
                if config.get(f'{role}_config_backup'):
                    backup_key = f"{role}_backup_1"
                    load_role(backup_key, f'{role}_config_backup')
                    self.backup_fleets[role].append(backup_key)
                for i in range(2, 10):
                    conf_key = f'{role}_config_backup_{i}'
                    if config.get(conf_key):
                        backup_key = f"{role}_backup_{i}"
                        load_role(backup_key, conf_key)
                        self.backup_fleets[role].append(backup_key)

            logger.info(f"Fleet Status -> Miner Backups: {len(self.backup_fleets['miner'])} | Evolver Backups: {len(self.backup_fleets['evolver'])}")
        except Exception as e:
            logger.critical(f"初始化失败: {e}"); raise

    def _get_billing_date(self, client_key):
        model_name = self.models.get(client_key, "").lower()
        now_utc = datetime.now(timezone.utc)
        if "gemini" in model_name:
            billing_dt = now_utc - timedelta(hours=8)
            return billing_dt.strftime('%Y-%m-%d') + "_GEMINI"
        elif any(x in model_name for x in ["doubao", "deepseek", "qwen", "glm", "yi-"]):
            billing_dt = now_utc + timedelta(hours=8)
            return billing_dt.strftime('%Y-%m-%d') + "_CN"
        else:
            return now_utc.strftime('%Y-%m-%d') + "_UTC"

    def _check_budget_availability(self, client_key):
        try:
            config = load_system_config()
            if "llm_budgets" not in config: config["llm_budgets"] = {}
            
            if client_key not in config["llm_budgets"]:
                default_limit = 3000 if "miner" in client_key and "backup" not in client_key else 1000
                config["llm_budgets"][client_key] = {
                    "daily_limit": default_limit, 
                    "used_today": 0, 
                    "last_used_date_utc": "INIT"
                }
                save_system_config(config)

            budget = config["llm_budgets"][client_key]
            current_billing_date = self._get_billing_date(client_key)
            last_record_date = budget.get("last_used_date_utc", "1970-01-01")

            if current_billing_date != last_record_date:
                budget["used_today"] = 0
                budget["last_used_date_utc"] = current_billing_date
                save_system_config(config)
                logger.info(f"[{client_key}] 新账单周期 ({current_billing_date})，预算已自动重置。")

            if budget["used_today"] >= budget.get("daily_limit", 0):
                return False 
            
            return True
        except Exception as e:
            logger.error(f"[{client_key}] 预算检查错误: {e}")
            return False 

    def _consume_budget(self, client_key, role):
        """ v17.0: 扣费并更新活跃节点记录 """
        try:
            config = load_system_config()
            # 1. 扣费
            if "llm_budgets" in config and client_key in config["llm_budgets"]:
                config["llm_budgets"][client_key]["used_today"] += 1
            
            # 2. 记录谁是活跃的 (Active Node)
            if "active_nodes" not in config: config["active_nodes"] = {}
            
            # 如果当前活跃节点变了，记录下来
            if config["active_nodes"].get(role) != client_key:
                config["active_nodes"][role] = client_key
                logger.info(f"[{role}] 活跃节点已更新为: {client_key}")
            
            save_system_config(config)
        except Exception as e:
            logger.error(f"[{client_key}] 扣费/状态更新失败: {e}")

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        prompt = self._build_miner_prompt(fields, operators, guidance, failed_examples)
        return self._call_fleet('miner', prompt)

    def generate_evolved_alpha_idea(self, base_obj, guidance=None):
        prompt = self._build_evolver_prompt(base_obj, guidance)
        return self._call_fleet('evolver', prompt, json_mode=True)

    def _call_fleet(self, role, prompt, json_mode=False):
        candidates = [role] + self.backup_fleets.get(role, [])
        
        for client_key in candidates:
            cb = self.circuit_breaker.get(client_key, {})
            if cb.get('fails', 0) >= self.CB_THRESHOLD:
                if time.time() - cb.get('last_fail_time', 0) < self.CB_TIMEOUT:
                    if client_key == role: logger.info(f"[{role}] 主力熔断中，跳过...")
                    continue
                else:
                    cb['fails'] = 0

            if not self._check_budget_availability(client_key):
                continue

            content, error = self._attempt_request(client_key, prompt, role)
            
            if content:
                self.circuit_breaker[client_key]['fails'] = 0
                # v17.0: 传入 role 以便记录状态
                self._consume_budget(client_key, role)
                
                if client_key != role:
                    model_used = self.models.get(client_key, "Unknown")
                    logger.info(f"[{role}] 备用节点 {client_key} ({model_used}) 调用成功！")
                
                if json_mode: return self._parse_json(content)
                else:
                    idea = self._extract_expression(content)
                    return {"expression": idea, "settings": {}} if idea else None
            else:
                self.circuit_breaker[client_key]['fails'] += 1
                self.circuit_breaker[client_key]['last_fail_time'] = time.time()
                logger.warning(f"[{client_key}] 调用失败: {str(error)[:100]}")

        logger.critical(f"[{role}] 🚨 舰队全灭！休眠 60s...")
        time.sleep(60)
        return None

    def _attempt_request(self, client_key, prompt, role):
        client = self.clients.get(client_key)
        model_name = self.models.get(client_key)
        if not client or not model_name: return None, "NO_CONFIG"
        
        extra_args = {}
        if "deepseek" in model_name.lower():
            extra_args['extra_body'] = {"enable_thinking": False}
        
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.9 if 'miner' in role else 0.7,
                **extra_args
            )
            return resp.choices[0].message.content.strip(), None
        except Exception as e:
            return None, str(e)

    def _extract_expression(self, text):
        try:
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            code_blocks = re.findall(r'```(?:python)?(.*?)```', text, re.DOTALL)
            if code_blocks:
                candidate = code_blocks[-1].strip()
                if ";" in candidate: return candidate.split(';')[0].strip() + ';'
                return candidate.strip() + ';'
            if ";" in text:
                pre = text.split(';')[0]
                cand = pre.split('\n')[-1].strip()
                cand = re.sub(r'^(?:\d+\.|Here is the code:|Code:|Expression:)\s*', '', cand, flags=re.IGNORECASE)
                if "=" in cand: cand = cand.split("=")[-1].strip()
                if "(" in cand and ")" in cand: return cand + ';'
            return None
        except: return None

    def _parse_json(self, text):
        try:
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            if "```" in text: text = text.split("```json")[1].split("```")[0]
            data = json.loads(text)
            if 'expression' in data: 
                data['expression'] = data['expression'].strip().replace('`','').split(';')[0] + ';'
                data['settings'] = {}
                return data
        except: pass
        return None

    def _build_miner_prompt(self, fields, ops, guidance, failed):
        op_str = ", ".join(ops[:20])
        return f"Create a WorldQuant alpha using: {', '.join(fields)}. Ops: {op_str}. Output ONLY the expression code ending with ;. NO explanations."

    def _build_evolver_prompt(self, base, guidance):
        return f"Evolve alpha: {base.get('expression')}. Output JSON."