import logging
import json
import re
import time
from openai import OpenAI
from datetime import datetime, timezone, timedelta
from utils import load_system_config, save_system_config

logger = logging.getLogger(__name__)

class LLMProvider:
    def __init__(self, api_config_path):
        logger.critical("🐛🐛🐛 [DEBUG MODE] 显微镜调试版已启动！ 🐛🐛🐛")
        self.api_config_path = api_config_path
        self.clients = {} 
        self.models = {}
        self.circuit_breaker = {} 
        self.CB_THRESHOLD = 2       
        self.CB_TIMEOUT = 600       
        self.backup_fleets = {'miner': [], 'evolver': []}
        self.FORBIDDEN_KEYWORDS = {'beta', 'indneutral_beta', 'cap', 'industry', 'sector'}
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
        except Exception as e: logger.critical(f"Init failed: {e}"); raise

    def _check_budget_availability(self, client_key):
        return True # 调试期间强制通过预算

    def _consume_budget(self, client_key, role):
        pass

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        return self._call_fleet('miner', "DEBUG_PROMPT")

    def generate_evolved_alpha_idea(self, base_obj, guidance=None):
        prompt = f"Evolve alpha: {base_obj.get('expression')}. Output JSON."
        return self._call_fleet('evolver', prompt, json_mode=True)

    def _call_fleet(self, role, prompt, json_mode=False):
        candidates = [role] + self.backup_fleets.get(role, [])
        for client_key in candidates:
            cb = self.circuit_breaker.get(client_key, {})
            fails = cb.get('fails', 0)
            is_primary_node = (client_key == role)

            # 调试日志：打印当前状态
            logger.warning(f"🔍 [DEBUG] Checking {client_key} | Fails: {fails} | Primary: {is_primary_node}")

            if fails >= self.CB_THRESHOLD:
                if time.time() - cb.get('last_fail_time', 0) < self.CB_TIMEOUT:
                    if is_primary_node: logger.info(f"[{role}] Skipping melted primary...")
                    continue
                else:
                    if is_primary_node: logger.info(f"[{role}] Probing primary...")

            content, error = self._attempt_request(client_key, prompt, role)
            
            # 🔥🔥🔥 核心调试点：打印变量类型和内容 🔥🔥🔥
            logger.critical(f"🕵️ [VAR CHECK] content='{content}' (Type: {type(content)}) | error='{error}' (Type: {type(error)})")

            result_data = None
            soft_failure = False

            if content:
                logger.info("   -> Branch: if content (TRUE)")
                if json_mode: result_data = self._parse_json(content)
                else: result_data = {"expression": content + ";", "settings": {}} # 简化逻辑
            elif error is None:
                logger.info("   -> Branch: elif error is None (TRUE) -> Setting soft_failure=True")
                soft_failure = True
            else:
                logger.info("   -> Branch: else (Hard Failure)")

            if result_data:
                self.circuit_breaker[client_key]['fails'] = 0
                return result_data
            else:
                if is_primary_node and soft_failure:
                    logger.warning(f"🛑 [DEBUG] Soft failure detected on primary. SLEEPING 10s...")
                    time.sleep(10)
                    return None 
                
                self.circuit_breaker[client_key]['fails'] += 1
                self.circuit_breaker[client_key]['last_fail_time'] = time.time()
                
                # 强制打印 fail_reason 的来源
                reason = "SOFT_FAIL" if soft_failure else f"HARD_FAIL({str(error)})"
                logger.warning(f"❌ [DEBUG] Failure registered. Reason: {reason}")

        logger.critical(f"🚨 Fleet exhausted.")
        time.sleep(60)
        return None

    def _attempt_request(self, client_key, prompt, role):
        client = self.clients.get(client_key)
        model = self.models.get(client_key)
        try:
            # 模拟请求以排除网络干扰，或者保留真实请求
            resp = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], max_tokens=100
            )
            if not resp or not resp.choices: return "", None
            content = resp.choices[0].message.content
            # 强制返回空字符串如果是 None
            return (content.strip() if content else ""), None
        except Exception as e:
            return None, str(e)

    def _parse_json(self, text):
        try:
            if "```" in text: text = text.split("```json")[1].split("```")[0]
            return json.loads(text)
        except: return None
