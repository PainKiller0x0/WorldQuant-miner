# --- llm_provider.py v17.9 (Final Force Fix) ---
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
        logger.critical("🚀🚀🚀 [v17.9 RELOADED] 强制修复版已加载！Empty Response 逻辑已生效！ 🚀🚀🚀")
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
            return (now_utc - timedelta(hours=8)).strftime('%Y-%m-%d') + "_GEMINI"
        elif any(x in model_name for x in ["doubao", "deepseek", "qwen", "glm", "yi-"]):
            return (now_utc + timedelta(hours=8)).strftime('%Y-%m-%d') + "_CN"
        else:
            return now_utc.strftime('%Y-%m-%d') + "_UTC"

    def _check_budget_availability(self, client_key):
        try:
            config = load_system_config()
            if "llm_budgets" not in config: config["llm_budgets"] = {}
            if client_key not in config["llm_budgets"]:
                default_limit = 3000 if "miner" in client_key and "backup" not in client_key else 1000
                config["llm_budgets"][client_key] = {"daily_limit": default_limit, "used_today": 0, "last_used_date_utc": "INIT"}
                save_system_config(config)
            budget = config["llm_budgets"][client_key]
            current_billing_date = self._get_billing_date(client_key)
            if current_billing_date != budget.get("last_used_date_utc", "1970-01-01"):
                budget["used_today"] = 0
                budget["last_used_date_utc"] = current_billing_date
                save_system_config(config)
                logger.info(f"[{client_key}] 新账单周期 ({current_billing_date})，预算已自动重置。")
            return budget["used_today"] < budget.get("daily_limit", 0)
        except Exception as e:
            logger.error(f"[{client_key}] 预算检查错误: {e}")
            return False 

    def _consume_budget(self, client_key, role):
        try:
            config = load_system_config()
            if "llm_budgets" in config and client_key in config["llm_budgets"]:
                config["llm_budgets"][client_key]["used_today"] += 1
            if "active_nodes" not in config: config["active_nodes"] = {}
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
        for i, client_key in enumerate(candidates):
            cb = self.circuit_breaker.get(client_key, {})
            fails = cb.get('fails', 0)
            last_fail_time = cb.get('last_fail_time', 0)
            is_probing = False
            is_primary_node = (client_key == role)

            if fails >= self.CB_THRESHOLD:
                if time.time() - last_fail_time < self.CB_TIMEOUT:
                    if is_primary_node: logger.info(f"[{role}] 主力熔断中 (剩余 {int(self.CB_TIMEOUT - (time.time() - last_fail_time))}s)，切换备用...")
                    continue
                else:
                    is_probing = True
                    if is_primary_node: logger.info(f"[{role}] 主力熔断冷却结束，尝试探测一次...")

            if not self._check_budget_availability(client_key): continue

            content, error = self._attempt_request(client_key, prompt, role)
            result_data = None
            soft_failure = False

            if content:
                if json_mode: result_data = self._parse_json(content)
                else:
                    idea = self._extract_expression(content)
                    if idea:
                        if self._contains_forbidden_keywords(idea):
                            logger.warning(f"[{client_key}] ❌ 生成了违禁词，视为无效: {idea[:30]}...")
                            soft_failure = True
                        else: result_data = {"expression": idea, "settings": {}}
                    else:
                         logger.warning(f"[{client_key}] 返回内容无法解析为代码...")
                         soft_failure = True
            elif error is None:
                logger.warning(f"[{client_key}] ❌ 返回内容为空字符串 (Empty Response)，视为 Soft Failure...")
                soft_failure = True

            if result_data:
                self.circuit_breaker[client_key]['fails'] = 0
                self._consume_budget(client_key, role)
                if not is_primary_node:
                    model_used = self.models.get(client_key, "Unknown")
                    logger.info(f"[{role}] 🛡️ 备用节点 {client_key} ({model_used}) 救援成功！")
                return result_data
            else:
                if is_primary_node and soft_failure:
                    logger.warning(f"[{role}] 主力节点 Soft Failure (内容无效/空白)，不计入熔断。休眠 10s 避免死磕...")
                    time.sleep(10)
                    return None 
                
                self.circuit_breaker[client_key]['fails'] += 1
                current_fails = self.circuit_breaker[client_key]['fails']
                self.circuit_breaker[client_key]['last_fail_time'] = time.time()
                fail_reason = "无法解析/违禁词/空白" if soft_failure else str(error)[:100]
                
                if is_probing: logger.warning(f"[{client_key}] 探测失败 (Reason: {fail_reason})，继续保持熔断状态。")
                else: logger.warning(f"[{client_key}] 调用失败 ({current_fails}/{self.CB_THRESHOLD}): {fail_reason}")

                if is_primary_node and current_fails < self.CB_THRESHOLD:
                    logger.info(f"[{role}] 主力节点网络错误 ({current_fails}/{self.CB_THRESHOLD})，未达熔断阈值。暂不切换备用。")
                    return None 
                if is_primary_node:
                     logger.warning(f"[{role}] 🚨 主力节点网络已连续崩溃 {current_fails} 次，触发熔断！正在呼叫备用舰队...")

        logger.critical(f"[{role}] 🚨 舰队全灭 (主力熔断且备用无效)！休眠 60s...")
        time.sleep(60)
        return None

    def _contains_forbidden_keywords(self, code):
        if not code: return False
        return not set(re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', code)).isdisjoint(self.FORBIDDEN_KEYWORDS)

    def _attempt_request(self, client_key, prompt, role):
        client = self.clients.get(client_key)
        model_name = self.models.get(client_key)
        if not client or not model_name: return None, "NO_CONFIG"
        try:
            resp = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}],
                max_tokens=1000, temperature=0.9 if client_key == 'miner' else 0.7
            )
            if not resp or not resp.choices or not resp.choices[0].message: return "", None
            content = resp.choices[0].message.content
            return (content.strip() if content else ""), None
        except Exception as e: return None, str(e)

    def _extract_expression(self, text):
        try:
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            code_blocks = re.findall(r'```(?:python|c\+\+|code)?(.*?)```', text, re.DOTALL | re.IGNORECASE)
            if code_blocks:
                candidate = re.sub(r'^\d+\.\s*', '', code_blocks[-1].strip(), flags=re.MULTILINE)
                if ";" in candidate: return candidate.split(';')[0].strip() + ';'
                return candidate.strip() + ';'
            if ";" in text:
                for line in reversed(text.split('\n')):
                    if ';' in line and len(line) > 5:
                        cand = re.sub(r'^(?:Alpha|Expression|Code|Here)\s*[:=]\s*', '', line.strip(), flags=re.IGNORECASE)
                        if "=" in cand: cand = cand.split("=")[-1].strip()
                        return cand.split(';')[0].strip() + ';'
            return None
        except: return None

    def _parse_json(self, text):
        try:
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            if "```" in text: 
                match = re.search(r'```(?:json)?(.*?)```', text, re.DOTALL | re.IGNORECASE)
                if match: text = match.group(1)
            data = json.loads(text)
            if 'expression' in data: 
                data['expression'] = data['expression'].strip().replace('`','').split(';')[0] + ';'
                data['settings'] = {}
                return data
        except: pass
        return None

    def _build_miner_prompt(self, fields, ops, guidance, failed):
        return f"Create a WorldQuant alpha using: {', '.join(fields)}. Ops: {', '.join(ops[:20])}. Output ONLY the expression code ending with ;. NO explanations. DO NOT USE: {', '.join(list(self.FORBIDDEN_KEYWORDS))}."

    def _build_evolver_prompt(self, base, guidance):
        return f"Evolve alpha: {base.get('expression')}. Output JSON."
