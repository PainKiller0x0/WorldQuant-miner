# --- llm_provider.py v17.2.1.2 (Fix: Expanded Desperate Keywords for Miner) ---
import logging
import json
import re
import random
import os
from openai import OpenAI
from datetime import datetime, timezone, timedelta
import time
from filelock import FileLock
from utils import load_system_config, save_system_config

logger = logging.getLogger(__name__)

class LLMProvider:
    def __init__(self, api_config_path):
        logger.critical("🔥 [v17.2.1.2 DESPERATE+] LLMProvider 升级：绝望词库已扩充 (覆盖 Miner 冷门算子)")
        
        self.api_config_path = api_config_path
        self.invalid_functions_file = "invalid_functions.json" 
        self.invalid_functions_lock_file = "invalid_functions.json.lock"
        
        self.clients = {} 
        self.models = {}
        self.circuit_breaker = {} 
        self.CB_THRESHOLD = 2       
        self.CB_TIMEOUT = 600       
        self.backup_fleets = {'miner': [], 'evolver': []}
        
        self.BASE_FORBIDDEN = {
            'beta', 'indneutral_beta', 'cap', 'industry', 'sector', 'group', 
            'market', 'estu', 'fnd', 'sest', 'sf', 'mkt', 'sec'
        }
        self._load_config()

    def _load_config(self):
        try:
            with open(self.api_config_path, 'r') as f: config = json.load(f)
            
            def load_role(role_key, config_key):
                conf = config.get(config_key, config) 
                if not conf or 'api_key' not in conf: return
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

    def _get_dynamic_forbidden_keywords(self):
        forbidden = self.BASE_FORBIDDEN.copy()
        try:
            if os.path.exists(self.invalid_functions_file):
                lock = FileLock(self.invalid_functions_lock_file, timeout=5)
                with lock:
                    with open(self.invalid_functions_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content:
                            data = json.loads(content)
                            for func_name, count in data.items():
                                if count >= 2: 
                                    forbidden.add(func_name)
        except Exception:
            pass 
        return forbidden

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
                default_limit = 3000 if "miner" in client_key else 1000
                config["llm_budgets"][client_key] = {
                    "daily_limit": default_limit, 
                    "used_today": 0, 
                    "last_used_date_utc": "INIT"
                }
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
        allowed = set(fields or []) | set(operators or [])
        return self._call_fleet('miner', prompt, json_mode=False, allowed_identifiers=allowed)

    def generate_evolved_alpha_idea(self, base_obj, guidance=None):
        prompt = self._build_evolver_prompt(base_obj, guidance)
        return self._call_fleet('evolver', prompt, json_mode=False)

    def _call_fleet(self, role, prompt, json_mode=False, allowed_identifiers=None):
        candidates = [role] + self.backup_fleets.get(role, [])
        
        for i, client_key in enumerate(candidates):
            cb = self.circuit_breaker.get(client_key, {})
            fails = cb.get('fails', 0)
            last_fail_time = cb.get('last_fail_time', 0)
            is_primary_node = (client_key == role)

            if fails >= self.CB_THRESHOLD:
                if time.time() - last_fail_time < self.CB_TIMEOUT:
                    if is_primary_node: 
                        logger.info(f"[{role}] 主力熔断中，切换备用...")
                    continue
                else:
                    pass 

            if not self._check_budget_availability(client_key):
                continue

            content, error = self._attempt_request(client_key, prompt, role)
            
            result_data = None
            soft_failure = False

            if content:
                if json_mode:
                    result_data = self._parse_json(content)
                    if not result_data: soft_failure = True
                else:
                    idea = self._extract_expression(content)
                    if idea:
                        dynamic_forbidden = self._get_dynamic_forbidden_keywords()
                        tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', idea)
                        unknown = sorted(set(tokens) - set(allowed_identifiers or set()) - {
                            'and', 'or', 'not', 'true', 'false', 'nan', 'inf'
                        }) if allowed_identifiers else []
                        if unknown:
                            logger.warning(f"[{client_key}] ❌ 包含未提供的字段/算子(本地拦截): {unknown[:8]}")
                            soft_failure = True
                        elif not set(tokens).isdisjoint(dynamic_forbidden):
                            logger.warning(f"[{client_key}] ❌ 包含违禁词(本地拦截)，视为 Soft Failure")
                            soft_failure = True
                        else:
                            result_data = {"expression": idea, "settings": {}}
                    else:
                         logger.warning(f"[{client_key}] 无法提取代码，视为 Soft Failure")
                         # [v17.2.1.2] 确保这里打印失败原文，方便调试
                         logger.warning(f"[{client_key}] 失败原文片段: {content[:100].replace(chr(10), ' ')}...")
                         soft_failure = True
            elif error is None:
                logger.warning(f"[{client_key}] ❌ 空响应 (Empty Response)，视为 Soft Failure")
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
                    logger.warning(f"[{role}] 主力节点 Soft Failure，不计入熔断。😴 休眠 10s...")
                    time.sleep(10)
                    return None 
                
                self.circuit_breaker[client_key]['fails'] += 1
                self.circuit_breaker[client_key]['last_fail_time'] = time.time()
                
                fail_reason = "Soft Failure" if soft_failure else str(error)[:100]
                logger.warning(f"[{client_key}] 调用失败 ({self.circuit_breaker[client_key]['fails']}/{self.CB_THRESHOLD}): {fail_reason}")

                if is_primary_node:
                    if self.circuit_breaker[client_key]['fails'] < self.CB_THRESHOLD: 
                        return None
                    else: 
                        logger.warning(f"[{role}] 🚨 主力熔断！呼叫备用...")

        logger.critical(f"[{role}] 🚨 舰队全灭！休眠 60s...")
        time.sleep(60)
        return None

    def _attempt_request(self, client_key, prompt, role):
        client = self.clients.get(client_key)
        model_name = self.models.get(client_key)
        if not client or not model_name: return None, "NO_CONFIG"
        
        extra_args = {}
        # GLM-4.7 defaults to thinking mode. Miner/Evolver only need a short
        # FASTEXPR, so disable reasoning to keep the output within the budget.
        if model_name.lower().startswith('glm-4.7'):
            extra_args['extra_body'] = {'thinking': {'type': 'disabled'}}
        
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.9 if 'miner' in role else 0.7,
                **extra_args
            )
            if not resp or not resp.choices or not resp.choices[0].message: return "", None
            content = resp.choices[0].message.content
            return (content.strip() if content else ""), None
        except Exception as e:
            return None, str(e)

    def _extract_expression(self, text):
        try:
            # 0. 移除思维链
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            
            # 1. 标准 Markdown
            code_blocks = re.findall(r'```(?:python|c\+\+|code|params)?(.*?)```', text, re.DOTALL | re.IGNORECASE)
            if code_blocks:
                return self._finalize_expression(code_blocks[-1])

            # 2. 内联代码
            inline_code = re.findall(r'`([^`]+)`', text)
            if inline_code:
                candidates = [c for c in inline_code if '(' in c and ')' in c]
                if candidates:
                    return self._finalize_expression(candidates[-1])

            # 3. 分号回溯 (Golden Retriever)
            if ";" in text:
                last_semi_idx = text.rfind(';')
                potential_raw = text[:last_semi_idx+1]
                lines = potential_raw.split('\n')
                buffer = []
                stop_patterns = [
                    r'alpha\s*[:=]', r'code\s*[:=]', r'expression\s*[:=]', 
                    r'here\s*is', r'output\s*[:=]', r'formula\s*[:=]', r'result\s*[:=]'
                ]
                for line in reversed(lines):
                    hit_stop = False
                    cleaned_line = line
                    for pat in stop_patterns:
                        match = re.search(pat, line, re.IGNORECASE)
                        if match:
                            cleaned_line = line[match.end():].strip()
                            hit_stop = True
                            break
                    if hit_stop:
                        if cleaned_line: buffer.insert(0, cleaned_line)
                        break 
                    buffer.insert(0, line)
                    if len(buffer) > 20: break
                candidate = "\n".join(buffer)
                return self._finalize_expression(candidate)

            # 4. [v17.2.1.2] 绝望回溯 (Desperate Fallback + Expanded Dictionary)
            # 扩充词库，覆盖 Miner 可能用到的冷门操作符
            wq_keywords = [
                # 核心
                'rank(', 'ts_', 'multiply(', 'divide(', 'add(', 'subtract(', 'correlation(', 'decay_linear(',
                # 统计/基础
                'std_dev(', 'mean(', 'sum(', 'product(', 'max(', 'min(', 'abs(', 'sign(', 'power(', 'log(', 'zscore(',
                # 逻辑/条件
                'if_else(', 'signed_power(', 'sigmoid(', 'tanh(',
                # 分组/行业
                'group_', 'industry_', 'sector_'
            ]
            
            first_kw_idx = len(text)
            found_any = False
            for kw in wq_keywords:
                idx = text.find(kw)
                if idx != -1:
                    found_any = True
                    if idx < first_kw_idx:
                        first_kw_idx = idx
            
            if found_any and first_kw_idx < len(text):
                candidate = text[first_kw_idx:]
                last_paren = candidate.rfind(')')
                if last_paren != -1:
                    candidate = candidate[:last_paren+1]
                
                final_alpha = self._finalize_expression(candidate)
                logger.warning(f"🔥 触发绝望提取模式，成功抢救 Alpha:\n{final_alpha}")
                return final_alpha

            return None
        except Exception as e:
            logger.warning(f"Extraction error: {e}")
            return None

    def _finalize_expression(self, code):
        code = code.strip()
        if "=" in code:
            code = re.sub(r'^(?:alpha|expression|res|code)\s*=\s*', '', code, flags=re.IGNORECASE)
        code = re.sub(r'^\d+\.\s*', '', code, flags=re.MULTILINE)
        
        if not code.endswith(';'):
            code += ';'
        
        code = code.replace('`', '')
        code = " ".join(code.split())
        return code

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
        op_str = ", ".join(ops[:25]) 
        dynamic_forbidden = self._get_dynamic_forbidden_keywords()
        forbidden_str = ", ".join(sorted(list(dynamic_forbidden)))
        
        prompt = (
            f"Role: WorldQuant Alpha Miner.\n"
            f"Task: Generate a valid alpha expression using available data.\n"
            f"Inputs: {', '.join(fields)}\n"
            f"Operators: {op_str}...\n"
        )
        
        if failed and isinstance(failed, list) and len(failed) > 0:
            samples = random.sample(failed, min(3, len(failed)))
            prompt += f"\n🚫 AVOID these failed patterns (Overfitting/High Correlation):\n"
            for i, s in enumerate(samples):
                prompt += f"   - {s}\n"

        if guidance and isinstance(guidance, list) and len(guidance) > 0:
            prompt += f"\n💡 Strategy Tip: Try incorporating high-performing operators like: {', '.join(guidance)}.\n"

        prompt += (
            f"\nConstraints:\n"
            f"1. Output ONLY the expression code ending with ';'.\n"
            f"2. NO explanations. NO markdown wrapping needed, just the code.\n"
            f"3. ❌ STRICTLY FORBIDDEN: {forbidden_str}.\n"
            f"4. Use only the exact field and operator names listed above; never invent, concatenate, or rename identifiers.\n"
            f"5. Use ordinary decimal constants only (for example 0.000001), never scientific notation such as 1e-8.\n"
            f"6. Make it mathematically diverse.\n"
        )
        return prompt

    def _build_evolver_prompt(self, base_obj, guidance):
        base_expression = base_obj.get('expression')
        dynamic_forbidden = self._get_dynamic_forbidden_keywords()
        forbidden_str = ", ".join(sorted(list(dynamic_forbidden)))
        
        special_instruction = base_obj.get('_special_guidance_high_corr', "")
        
        prompt = (
            f"Role: WorldQuant Alpha Evolver.\n"
            f"Task: Mutate the following alpha to improve performance.\n"
            f"Original Alpha: {base_expression}\n"
        )
        
        if special_instruction:
            prompt += f"\n🔥 CRITICAL INSTRUCTION: {special_instruction}\n"
        
        if guidance and isinstance(guidance, list) and len(guidance) > 0:
            prompt += f"\n💡 Evolution Hint: Try introducing patterns like {', '.join(guidance)} to diversify logic.\n"
        
        prompt += (
            f"\nInstructions:\n"
            f"1. Output ONLY the new alpha expression code ending with ';'.\n"
            f"2. Do NOT output JSON. Do NOT output explanations.\n"
            f"3. ❌ STRICTLY FORBIDDEN: {forbidden_str}.\n"
            f"4. Try to introduce new operators or logic.\n"
        )
        return prompt
