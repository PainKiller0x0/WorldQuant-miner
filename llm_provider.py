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
        self.clients = {} # 存储不同角色的 client
        self.models = {}  # 存储不同角色的 model name
        
        self._load_config_and_init_clients()

    def _load_config_and_init_clients(self):
        """
        (v14.0) 加载双轨制配置 (miner_config / evolver_config)
        """
        try:
            with open(self.api_config_path, 'r') as f: 
                config = json.load(f)
            
            # 1. 初始化 Miner 客户端 (挖掘者)
            miner_conf = config.get('miner_config', config) # 回退到旧格式
            self.models['miner'] = miner_conf.get('model_name', 'gemini-2.0-flash-lite-preview-02-05')
            self.clients['miner'] = OpenAI(
                api_key=miner_conf.get('api_key', 'painkiller0x0'), 
                base_url=miner_conf.get('base_url')
            )
            logger.info(f"[Model Fleet] Miner 引擎就绪: {self.models['miner']}")

            # 2. 初始化 Evolver 客户端 (进化者)
            # 如果没有 evolver_config, 默认复用 miner 的配置，但可以使用不同的模型
            evolver_conf = config.get('evolver_config', miner_conf)
            self.models['evolver'] = evolver_conf.get('model_name', 'gemini-2.0-pro-exp-02-11')
            self.clients['evolver'] = OpenAI(
                api_key=evolver_conf.get('api_key', miner_conf.get('api_key')), 
                base_url=evolver_conf.get('base_url', miner_conf.get('base_url'))
            )
            logger.info(f"[Model Fleet] Evolver 引擎就绪: {self.models['evolver']}")

        except Exception as e:
            logger.critical(f"初始化模型舰队失败: {e}")
            raise

    def _check_budget(self, role):
        """
        (v14.0) 双轨制预算检查 (Miner / Evolver 独立核算)
        """
        try:
            config = load_system_config()
            
            # 兼容性处理：如果没有 llm_budgets，初始化它
            if "llm_budgets" not in config:
                config["llm_budgets"] = {
                    "miner": {"daily_limit": 3000, "used_today": 0, "last_used_date_utc": "2024-01-01"},
                    "evolver": {"daily_limit": 1000, "used_today": 0, "last_used_date_utc": "2024-01-01"}
                }
            
            budget_info = config["llm_budgets"].get(role, config["llm_budgets"]["miner"]) # 默认回退到 miner
            
            limit = budget_info.get("daily_limit", 2000)
            used = budget_info.get("used_today", 0)
            last_date = budget_info.get("last_used_date_utc", "2024-01-01")
            
            today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')

            # 每日重置
            if today_utc != last_date:
                logger.info(f"[Watchdog A] ({role.upper()}) 新的一天! 重置预算。")
                used = 0
                config["llm_budgets"][role]["last_used_date_utc"] = today_utc
            
            # 检查限额
            if used >= limit:
                logger.warning(f"[Watchdog A] ({role.upper()}) 预算耗尽! ({used}/{limit})")
                return "BUDGET_EXHAUSTED"

            # 扣费
            used += 1
            config["llm_budgets"][role]["budget_used_today"] = used
            
            # 保存
            if not save_system_config(config):
                 logger.error(f"[Watchdog A] 保存 {role} 预算失败!")
            
            if used % 50 == 0:
                 logger.info(f"[Watchdog A] ({role.upper()}) 预算: {used}/{limit}")
                 
            return "OK"

        except Exception as e:
            logger.error(f"[Watchdog A] 预算检查错误 ({role}): {e}", exc_info=True)
            return "BUDGET_EXHAUSTED"

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        """
        (Miner 专用通道) - 使用 Flash-Lite
        """
        ROLE = 'miner'
        
        # 1. 预算检查
        if self._check_budget(ROLE) == "BUDGET_EXHAUSTED":
            return "BUDGET_EXHAUSTED"

        # 2. 构建 Prompt (逻辑保持 v13.0 不变)
        field_list = ", ".join(fields)
        core_ops = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']
        if not isinstance(operators, list): operators = []
        adv_ops = [op for op in operators if op not in core_ops]
        extra_ops = random.sample(adv_ops, min(len(adv_ops), 20)) if adv_ops else []
        combined_ops = core_ops + extra_ops
        op_list = ", ".join(combined_ops)

        prompt_lines = [
            "You are a world-class Quantitative Analyst creating alphas for WorldQuant.",
            "1. Use ONLY provided fields/operators.",
            "2. Expression MUST end with ';'.",
            "3. `ts_` functions need lookback int (e.g. `ts_mean(close, 10)`).",
            "4. Aim for 5-15 operators.",
            "5. Output ONLY the raw expression."
        ]
        
        if failed_examples:
            prompt_lines.append("\n**AVOID these FAILED patterns (High Self-Corr):**")
            for expr in failed_examples: prompt_lines.append(f"- `{expr[10:80]}...`")
        
        if guidance:
            prompt_lines.append(f"**Guidance:** Try patterns like: `{', '.join(guidance)}`")

        prompt_lines.extend([
            f"\n**Fields:** {field_list}",
            f"**Operators:** {op_list}",
            "\n**Generate ONE new Alpha:**"
        ])
        prompt = "\n".join(prompt_lines)

        # 3. 调用 API (使用 miner client)
        try:
            resp = self.clients[ROLE].chat.completions.create(
                model=self.models[ROLE],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200, temperature=0.95
            )
            idea = resp.choices[0].message.content.strip().replace('`', '').split(';')[0]
            return {"expression": idea + ';', "settings": {}} if idea else None
        except Exception as e:
            return self._handle_error(e, ROLE)

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        """
        (Evolver 专用通道) - 使用 Pro
        """
        ROLE = 'evolver'

        # 1. 预算检查
        if self._check_budget(ROLE) == "BUDGET_EXHAUSTED":
            return "BUDGET_EXHAUSTED"

        # 2. 构建 Prompt
        base_expr = base_alpha_obj.get('expression')
        settings = base_alpha_obj.get('performance', {}).get('settings', {})
        
        prompt_lines = [
            "You are an AI evolving investment strategies.",
            "Output ONLY a JSON object.",
            f"**Base Strategy:** `{base_expr}`",
            f"**Settings:** `{json.dumps(settings)}`"
        ]

        special_guidance = base_alpha_obj.get('_special_guidance_high_corr')
        if special_guidance:
            prompt_lines.append(f"**CRITICAL:** {special_guidance}")
            prompt_lines.append("PERFORM STRUCTURAL MUTATION. Do not just change numbers.")
        else:
            prompt_lines.append("Evolve the expression. Introduce new operators/fields.")

        prompt_lines.append("\n**Output Format:**\n```json\n{ \"expression\": \"...\", \"settings\": {} }\n```")
        prompt = "\n".join(prompt_lines)

        # 3. 调用 API (使用 evolver client)
        try:
            resp = self.clients[ROLE].chat.completions.create(
                model=self.models[ROLE],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300, temperature=0.7
            )
            return self._parse_json_response(resp.choices[0].message.content)
        except Exception as e:
            return self._handle_error(e, ROLE)

    def _handle_error(self, e, role):
        code = getattr(e, 'status_code', getattr(getattr(e, 'response', None), 'status_code', -1))
        if code in [429, 500]:
            logger.warning(f"[{role.upper()}] API Rate Limit/Error ({code}).")
            return "RATE_LIMIT"
        logger.error(f"[{role.upper()}] API Failed: {e}")
        return None

    def _parse_json_response(self, text):
        try:
            text = text.strip()
            match = re.search(r'```json\s*([\s\S]+?)\s*```', text)
            if match: text = match.group(1)
            
            data = json.loads(text)
            if 'expression' in data:
                expr = data['expression'].strip().replace('`', '').split(';')[0]
                if expr: 
                    data['expression'] = expr + ';'
                    data['settings'] = {} # 清空设置，由 evolver 负责突变
                    return data
            return None
        except:
            return None