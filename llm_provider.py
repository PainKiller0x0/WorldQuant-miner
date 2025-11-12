# --- llm_provider.py v14.0 (Model Fleet & Dual-Track Budget) ---
import logging
import json
import re
import random
from openai import OpenAI
from datetime import datetime, timezone
import time
from utils import load_system_config, save_system_config

logger = logging.getLogger(__name__)

class LLMProvider:
    def __init__(self, api_config_path):
        self.api_config_path = api_config_path
        self.clients = {} 
        self.models = {}
        self._load_config()

    def _load_config(self):
        try:
            with open(self.api_config_path, 'r') as f: config = json.load(f)
            
            # Miner 配置
            m_conf = config.get('miner_config', config)
            self.models['miner'] = m_conf.get('model_name', 'gemini-2.5-flash-lite')
            self.clients['miner'] = OpenAI(api_key=m_conf.get('api_key'), base_url=m_conf.get('base_url'))
            
            # Evolver 配置
            e_conf = config.get('evolver_config', m_conf)
            self.models['evolver'] = e_conf.get('model_name', 'gemini-2.5-flash')
            self.clients['evolver'] = OpenAI(api_key=e_conf.get('api_key'), base_url=e_conf.get('base_url'))
            
            logger.info(f"Miner: {self.models['miner']} | Evolver: {self.models['evolver']}")
        except Exception as e:
            logger.critical(f"初始化失败: {e}"); raise

    def _check_budget(self, role):
        try:
            # 1. 加载配置 (如果失败 utils.py 会抛异常，中断流程，保护数据不被重置)
            config = load_system_config()
            
            # 初始化结构
            if "llm_budgets" not in config:
                config["llm_budgets"] = {
                    "miner": {"daily_limit": 3000, "used_today": 0, "last_used_date_utc": "2024-01-01"},
                    "evolver": {"daily_limit": 1000, "used_today": 0, "last_used_date_utc": "2024-01-01"}
                }

            budget = config["llm_budgets"].get(role, config["llm_budgets"]["miner"])
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

            # 每日重置
            if today != budget.get("last_used_date_utc"):
                budget["used_today"] = 0
                budget["last_used_date_utc"] = today
                logger.info(f"[{role}] 新的一天，预算已重置。")

            if budget["used_today"] >= budget.get("daily_limit", 2000):
                logger.warning(f"[{role}] 预算耗尽 ({budget['used_today']})")
                return "BUDGET_EXHAUSTED"

            # 扣费
            budget["used_today"] += 1
            
            # 保存
            if not save_system_config(config):
                logger.error("保存预算失败") # 就算保存失败，内存里也加了，下次读取只要成功就是对的
            
            return "OK"
        except Exception as e:
            logger.error(f"预算检查错误: {e}"); return "BUDGET_EXHAUSTED"

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        role = 'miner'
        if self._check_budget(role) != "OK": return "BUDGET_EXHAUSTED"
        
        # (简化的 Prompt 构建逻辑，与 v13.0 保持一致)
        prompt = self._build_miner_prompt(fields, operators, guidance, failed_examples)
        return self._call_llm(role, prompt)

    def generate_evolved_alpha_idea(self, base_obj, guidance=None):
        role = 'evolver'
        if self._check_budget(role) != "OK": return "BUDGET_EXHAUSTED"
        
        prompt = self._build_evolver_prompt(base_obj, guidance)
        return self._call_llm(role, prompt, json_mode=True)

    def _call_llm(self, role, prompt, json_mode=False):
        try:
            resp = self.clients[role].chat.completions.create(
                model=self.models[role],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.9 if role == 'miner' else 0.7
            )
            content = resp.choices[0].message.content.strip()
            
            if json_mode:
                return self._parse_json(content)
            else:
                idea = content.replace('`', '').split(';')[0]
                return {"expression": idea + ';', "settings": {}} if idea else None
        except Exception as e:
            logger.error(f"LLM调用失败 ({role}): {e}")
            return "RATE_LIMIT" if "429" in str(e) else None

    def _parse_json(self, text):
        try:
            if "```" in text: text = text.split("```json")[1].split("```")[0]
            data = json.loads(text)
            if 'expression' in data: 
                data['expression'] = data['expression'].strip().replace('`','').split(';')[0] + ';'
                data['settings'] = {}
                return data
        except: pass
        return None

    def _build_miner_prompt(self, fields, operators, guidance, failed):
        # ... (这里省略 Prompt 字符串构建细节，与之前版本一致，重点是结构) ...
        # 为了确保代码完整可运行，这里提供一个基础版本
        op_str = ", ".join(operators[:20])
        return f"Create a WorldQuant alpha using: {', '.join(fields)}. Ops: {op_str}. Output ONLY the expression ending with ;"

    def _build_evolver_prompt(self, base, guidance):
        return f"Evolve this alpha: {base.get('expression')}. Output JSON: {{'expression': '...', 'settings': {{}}}}"