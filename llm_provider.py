# --- llm_provider.py ---
# LLM (Ollama/OpenAI) API 交互模块

import logging
import json
import re
import random
from openai import OpenAI

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

class LLMProvider:
    def __init__(self, api_config_path):
        self.model_name = "gemini-2.5-flash-lite"
        try:
            with open(api_config_path, 'r') as f: 
                config = json.load(f)
            self.client = OpenAI(api_key=config.get('api_key', 'painkiller0x0'), base_url=config['base_url'])
            logger.info(f"LLMProvider API client initialized for endpoint: {config['base_url']}")
            logger.info(f"将使用您指定的模型: {self.model_name}")
        except Exception as e:
            logger.critical(f"加载 API 配置或初始化 LLM 客户端失败: {e}")
            raise

    def generate_alpha_idea(self, fields, operators, guidance=None):
        field_list = ", ".join(fields)

        # --- v7.5 优化: 动态混合操作符 ---
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']

        # v7.8.1: 确保 operators 是列表
        if not isinstance(operators, list): operators = []
        advanced_operators = [op for op in operators if op not in core_operators]
        sample_size = 0
        extra_operators = []
        if advanced_operators: # v7.8.1: 确保 advanced_operators 不为空
             sample_size = min(len(advanced_operators), 20) # 最多抽取20个
             extra_operators = random.sample(advanced_operators, sample_size)

        combined_operators = core_operators + extra_operators
        operator_list = ", ".join(combined_operators)
        logger.info(f"本轮 Discover 将使用 {len(combined_operators)} 个操作符 (最多 13 核心 + {sample_size} 随机)。")
        # --- v7.5 结束 ---

        prompt_lines = [
            "You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.",
            "Follow these rules strictly:",
            "1.  **Use ONLY the provided fields and operators.**",
            "2.  **The expression MUST end with a semicolon (;).**",
            "3.  **IMPORTANT SYNTAX:** All functions starting with `ts_` (like `ts_corr`, `ts_mean`, etc.) MUST have a second integer argument for the lookback period (e.g., `ts_mean(close, 10)`).",
            "4.  **Complexity:** Try to keep operators below 15, but more complex and creative combinations are encouraged.",
            "5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression.",
            "6.  **Be Creative:** Do not just combine `close` and `vwap`. Use other fields like `cap`, `adv20`, or `returns`.",
            "**CRITICAL RULE:** Avoid high Self-Correlation (> 0.7). Your expression should be novel and change signal frequently." # v7.9
        ]

        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Our analysis suggests these patterns are successful: `{', '.join(guidance)}`. Try to incorporate some of these patterns.")

        prompt_lines.extend([
            f"**Available Data Fields:** {field_list}",
            f"**Allowed Operators:** {operator_list}", # v7.5
            "New Alpha Expression:"
        ])
        prompt = "\n".join(prompt_lines)

        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=150, temperature=0.95)
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
             # v7.8.1: 更严格的结尾检查和清理
            idea = idea.split(';')[0] # 取第一个分号前的部分
            if idea: idea += ';' # 确保以分号结尾
            else: return None # 如果为空则返回 None

            return {"expression": idea, "settings": {}}
        except Exception as e:
            # --- v9.0 重构: 检查状态码并返回信号，而不是调用冷却 ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code in [500, 429]:
                logger.warning(f"生成 Alpha 时检测到 LLM Gateway 错误 (Code: {status_code}): {e}。将返回 RATE_LIMIT 信号。")
                return "RATE_LIMIT"
            else:
                logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None
            # --- v9.0 结束 ---

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        base_expression = base_alpha_obj.get('expression')
        base_settings = base_alpha_obj.get('performance', {}).get('settings', {}) # v9.0: 移除 self.wq.default_settings 依赖
        base_score_info = base_alpha_obj.get('internal_score', 0.0) # v9.0: 依赖传入的分数

        prompt_lines = [
            "You are an AI machine that generates code. Your SOLE task is to evolve a given investment strategy for WorldQuant.",
            "You MUST output ONLY a single, valid JSON object in a markdown code block. Do NOT include any explanations, analysis, or introductory text.",
            f"**Base Strategy for Evolution:**",
            f"- Expression: `{base_expression}`",
            f"- Settings: `{json.dumps(base_settings)}`",
            f"- (Internal Score: {base_score_info:.3f})" # v7.9
        ]

        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Analysis suggests these patterns are successful: `{', '.join(guidance)}`. Your evolution should try to incorporate one of these patterns.")

        prompt_lines.extend([
            "\n**Task:** Apply ONE of the following evolution strategies. Your goal is to BREAK 'fitness > 1.0' by escaping local optima. Be creative and bold.",
            "1.  **Evolve Expression (HIGHLY PREFERRED):** Make a significant, creative change. Try to INTRODUCE 1-2 NEW operators or data fields (especially from the strategic guidance), or combine existing parts in a novel way. Do not just change a number.",
            "2.  **Evolve Settings (Low Priority):** Make a small, logical change to ONE numeric setting (`delay`, `decay`, `truncation`). Only do this if you cannot find a good expression evolution.",
            "**CRITICAL RULE:** Avoid high Self-Correlation (> 0.7). Your evolution *must* aim to reduce correlation if it is high, or keep it low.", # v7.9
            "\n**MANDATORY OUTPUT FORMAT:**",
            "Your entire response MUST be ONLY the raw JSON object inside a markdown code block. Example:",
            "```json",
            "{",
            '  "expression": "rank(ts_corr(close, vwap, 10));",',
            '  "settings": {}',
            "}",
            "```",
            "Evolved Strategy:"
        ])
        prompt = "\n".join(prompt_lines)

        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=300, temperature=0.7)
            response_text = chat_completion.choices[0].message.content.strip()

            json_match = re.search(r'```json\s*([\s\S]+?)\s*```', response_text)
            if not json_match:
                try:
                    evolved_strategy = json.loads(response_text)
                except json.JSONDecodeError:
                    logger.error(f"进化返回的内容中既不是JSON代码块，也不是合法的JSON: {response_text}")
                    return None
            else:
                json_str = json_match.group(1)
                try:
                    evolved_strategy = json.loads(json_str)
                except json.JSONDecodeError:
                     logger.error(f"进化返回的JSON代码块内容无效: {json_str}")
                     return None

            if not isinstance(evolved_strategy, dict) or 'expression' not in evolved_strategy or 'settings' not in evolved_strategy:
                logger.error("进化返回的JSON格式无效，缺少expression或settings键，或不是字典。")
                return None

            if 'expression' in evolved_strategy and isinstance(evolved_strategy['expression'], str):
                 expr = evolved_strategy['expression'].strip().replace('`', '')
                 expr = expr.split(';')[0]
                 if expr: evolved_strategy['expression'] = expr + ';'
                 else: evolved_strategy['expression'] = None

            if not evolved_strategy.get('expression'):
                 logger.error("进化返回的 expression 清理后为空。")
                 return None

            return evolved_strategy
        except Exception as e:
            # --- v9.0 重构: 检查状态码并返回信号，而不是调用冷却 ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code
            
            if status_code in [500, 429]:
                logger.warning(f"进化 Alpha 时检测到 LLM Gateway 错误 (Code: {status_code}): {e}。将返回 RATE_LIMIT 信号。")
                return "RATE_LIMIT"
            else:
                logger.error(f"从 API '进化' Alpha 策略失败: {e}")
            return None
            # --- v9.0 结束 ---