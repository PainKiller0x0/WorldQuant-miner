# --- llm_provider.py v13.0 (Dual Watchdog - Watchdog A) ---
import logging
import json
import re
import random
from openai import OpenAI
from datetime import datetime, timezone # v13.0: 新增
import time # v13.0: 新增

# --- v13.0: 新增导入 ---
from utils import load_system_config, save_system_config
# --- v13.0 结束 ---

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

    # --- v13.0: 看门狗 A (LLM 预算检查) ---
    def _check_llm_budget(self):
        """
        检查并更新 LLM 每日预算。
        - 如果预算充足，递增计数器并返回 "OK"。
        - 如果预算耗尽，返回 "BUDGET_EXHAUSTED"。
        - 实现了基于 UTC 日期的自动重置。
        """
        try:
            config = load_system_config()
            budget_config = config.get("llm_budget", {})
            
            daily_limit = budget_config.get("daily_budget_limit", 2000)
            used_today = budget_config.get("budget_used_today", 0)
            last_used_date_str = budget_config.get("budget_last_used_date_utc", "2024-01-01")
            
            # 关键：使用 UTC 日期进行比较
            today_utc_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

            # 1. 检查是否是新的一天 (UTC)
            if today_utc_str != last_used_date_str:
                logger.info(f"[Watchdog A] 检测到新 UTC 日期: {today_utc_str}。重置 LLM 每日预算。")
                used_today = 0
                config["llm_budget"]["budget_last_used_date_utc"] = today_utc_str
            
            # 2. 检查预算是否已用尽
            if used_today >= daily_limit:
                logger.critical(f"[Watchdog A] LLM 每日预算已用尽 ({used_today}/{daily_limit})。今天将不再调用 LLM API。")
                # (注意：alpha_generator 将负责处理此信号并进入休眠)
                return "BUDGET_EXHAUSTED"

            # 3. 预算充足，递增并保存
            used_today += 1
            config["llm_budget"]["budget_used_today"] = used_today
            
            if not save_system_config(config):
                 logger.error("[Watchdog A] 严重错误：更新 LLM 预算后保存 system_config.json 失败！")
            
            if used_today % 100 == 0 or used_today == 1:
                 logger.info(f"[Watchdog A] LLM 预算使用量: {used_today}/{daily_limit} (日期: {today_utc_str})")
                 
            return "OK"

        except Exception as e:
            logger.error(f"[Watchdog A] 检查 LLM 预算时发生严重错误: {e}", exc_info=True)
            # 出现异常时，安全起见，暂时阻止调用
            return "BUDGET_EXHAUSTED"
    # --- v13.0 结束 ---


    # --- v12.0: 签名变更，增加 failed_examples ---
    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        field_list = ", ".join(fields)

        # --- v7.5 优化: 动态混合操作符 ---
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']
        if not isinstance(operators, list): operators = []
        advanced_operators = [op for op in operators if op not in core_operators]
        sample_size = 0
        extra_operators = []
        if advanced_operators:
             sample_size = min(len(advanced_operators), 20)
             extra_operators = random.sample(advanced_operators, sample_size)
        combined_operators = core_operators + extra_operators
        operator_list = ", ".join(combined_operators)
        logger.info(f"本轮 Discover 将使用 {len(combined_operators)} 个操作符 (最多 13 核心 + {sample_size} 随机)。")
        # --- v7.5 结束 ---

        # --- v12.0: Enhanced Prompt with Feedback Loop ---
        prompt_lines = [
            "You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.",
            "Follow these rules strictly:",
            "1.  **Use ONLY the provided fields and operators.**",
            "2.  **The expression MUST end with a semicolon (;).**",
            "3.  **IMPORTANT SYNTAX:** All functions starting with `ts_` (like `ts_corr`, `ts_mean`, etc.) MUST have a second integer argument for the lookback period (e.g., `ts_mean(close, 10)`).",
            "4.  **Complexity:** Aim for 5-15 operators. Creative combinations are encouraged.",
            "5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression.",
            "6.  **Be Creative:** Do not just combine `close` and `vwap`. Use other fields like `cap`, `adv20`, or `returns`."
        ]
        
        # --- v12.0: 关键反馈循环 (Miner) ---
        if failed_examples and isinstance(failed_examples, list):
            logger.info(f"[LLM Discover] 注入 {len(failed_examples)} 个“高自相关”失败案例作为反面教材。")
            prompt_lines.extend([
                "\n**CRITICAL WARNING: AVOID FAILED PATTERNS!**",
                "The following expressions recently FAILED submission due to **High Self-Correlation**. Analyze their structure (e.g., simple `ts_rank` of `close`/`vwap`, simple `ts_delta` of prices) and **DO NOT** create anything structurally similar:",
            ])
            for expr in failed_examples:
                prompt_lines.append(f"- `... {expr[10:80]} ...`") # 截取中间部分
            prompt_lines.append("**Your signal MUST be significantly different from the failed examples above.**")
        else:
            # v9.4的旧提示 (作为回退)
            prompt_lines.extend([
                "7.  **CRITICAL RULE: Avoid high Self-Correlation!** WQ rejects Self-Correlation > 0.7. To lower correlation:",
                "    - Combine different operator types (e.g., trend `ts_` + momentum/reversal `rank`).",
                "    - Avoid overly simple transformations on `close` or `vwap` alone.",
                "    - Introduce faster-changing fields like `volume`, `turnover`, or `returns`.",
                "    - Ensure the signal changes reasonably often."
            ])
        # --- v12.0 结束 ---

        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Our analysis suggests these patterns are successful: `{', '.join(guidance)}`. Try to incorporate some of these patterns.")

        # v9.4: Add Few-Shot Examples (保留)
        example_1 = "rank(ts_mean(multiply(ts_corr(close, volume, 10), ts_delta(vwap, 5)), 15)) - rank(ts_std_dev(open, 30)) * rank(ts_delta(close, 1)) + rank(ts_delta(volume, 3)) * rank(ts_mean(close, 5)) + rank(ts_corr(open, close, 10)) + rank(ts_decay_linear(ts_corr(high, low, 20), 10));"
        example_2 = "rank(ts_corr(close, vwap, 10)) * rank(ts_mean(high, 5)) - rank(ts_std_dev(volume, 15) * ts_delta(close, 3) * rank(ts_mean(open, 7)) + ts_mean(close, 20) * rank(ts_mean(close, 30)) + ts_delta(close, 2) + ts_mean(open, 10) * rank(close) + ts_mean(vwap, 10)) * rank(ts_delta(close, 5)) + ts_decay_linear(rank(ts_corr(close, open, 20)), 10);"
        
        prompt_lines.extend([
            "\n**Here are examples of successful Alphas with good (low) Self-Correlation. Learn from their structure:**",
            f"- `{example_1}`",
            f"- `{example_2}`"
        ])

        prompt_lines.extend([
            f"\n**Available Data Fields:** {field_list}",
            f"**Allowed Operators:** {operator_list}",
            "\n**Generate ONE new Alpha Expression (different from the FAILED examples):**"
        ])
        
        prompt = "\n".join(prompt_lines)

        try:
            # --- v13.0: 看门狗 A 检查点 ---
            budget_status = self._check_llm_budget()
            if budget_status == "BUDGET_EXHAUSTED":
                return "BUDGET_EXHAUSTED"
            # --- v13.0 结束 ---

            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=200, temperature=0.95) 
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
             # v7.8.1: 更严格的结尾检查和清理
            idea = idea.split(';')[0] # 取第一个分号前的部分
            if idea: idea += ';' # 确保以分号结尾
            else: return None # 如果为空则返回 None

            return {"expression": idea, "settings": {}} # Settings are handled by AlphaGenerator now
        except Exception as e:
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code in [500, 429]:
                logger.warning(f"生成 Alpha 时检测到 LLM Gateway 错误 (Code: {status_code}): {e}。将返回 RATE_LIMIT 信号。")
                return "RATE_LIMIT"
            else:
                logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        base_expression = base_alpha_obj.get('expression')
        base_settings = base_alpha_obj.get('performance', {}).get('settings', {}) 
        base_score_info = base_alpha_obj.get('internal_score', 0.0) 

        # --- v12.0: Enhanced Evolve Prompt (for Ground Truth Failures) ---
        prompt_lines = [
            "You are an AI machine evolving WorldQuant investment strategies. Your task is to modify the given Alpha Expression.",
            "You MUST output ONLY a single, valid JSON object in a markdown code block. Do NOT include any explanations.",
            f"**Base Strategy for Evolution:**",
            f"- Expression: `{base_expression}`",
            f"- Settings: `{json.dumps(base_settings)}`",
            f"- (Internal Score: {base_score_info:.3f})"
        ]

        # v9.4: Combine strategic and specific guidance
        final_guidance = []
        if isinstance(guidance, list) and guidance:
             final_guidance.extend(guidance)
        
        # --- v12.0: 关键反馈循环 (Evolver) ---
        # (AlphaGenerator v12.0 将会注入一个更强烈的、基于“地面真相”的指导)
        high_corr_guidance = base_alpha_obj.get('_special_guidance_high_corr') 
        if high_corr_guidance:
            # v12.0: 这个指导现在可能来自"模拟" (v9.4) 或"地面真相" (v12.0)
            # 无论哪种，我们都强化这个提示。
            logger.warning(f"[LLM Evolve] 收到特殊指导: {high_corr_guidance}")
            prompt_lines.append(f"**WARNING:** {high_corr_guidance}")
            
            # --- v12.0: 强制“突变”指令 ---
            prompt_lines.append(
                "\n**Task: Perform STRUCTURAL MUTATION.** The parent alpha has a KNOWN FLAW (likely High Self-Correlation)."
            )
            prompt_lines.append(
                "   - **DO NOT** just change numbers (e.g., 10 -> 15). This WILL fail."
            )
            prompt_lines.append(
                "   - **YOU MUST** perform a major structural change: Introduce 1-2 NEW operators (like `ts_zscore`, `adv`, `ts_decay_linear`), combine different fields, or completely change the logic to escape this failed pattern."
            )
            # --- v12.0 结束 ---
        else:
            # 常规进化
            prompt_lines.extend([
                "\n**Task:** Evolve ONLY the Alpha Expression. Make a significant, creative change to escape local optima.",
                "   - Try to INTRODUCE 1-2 NEW operators or data fields, or combine parts in a novel way.",
                "   - Do not just change a number.",
                "**CRITICAL RULE: Avoid or Reduce high Self-Correlation!** WQ rejects Self-Correlation > 0.7.",
                "   - To lower correlation: Combine different operator types (e.g., trend `ts_` + reversal `rank`),",
                "   - avoid simple `close`/`vwap` transforms, use faster fields (`volume`, `returns`).",
            ])
        # --- v12.0 Prompt End ---


        prompt_lines.extend([
            "\n**MANDATORY OUTPUT FORMAT:**",
            "Your entire response MUST be ONLY the raw JSON object inside a markdown code block. Example:",
            "```json",
            "{",
            '  "expression": "rank(ts_corr(close, vwap, 10));"', 
            '  "settings": {}', # Settings key MUST be present but value should be empty
            "}",
            "```",
            "\n**Evolved Strategy (Expression ONLY):**"
        ])
        
        prompt = "\n".join(prompt_lines)

        try:
            # --- v13.0: 看门狗 A 检查点 ---
            budget_status = self._check_llm_budget()
            if budget_status == "BUDGET_EXHAUSTED":
                return "BUDGET_EXHAUSTED"
            # --- v13.0 结束 ---

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
                 
            # v9.4: Ensure settings is always returned empty, even if LLM hallucinates it
            evolved_strategy['settings'] = {} 

            return evolved_strategy
        except Exception as e:
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code
            
            if status_code in [500, 429]:
                logger.warning(f"进化 Alpha 时检测到 LLM Gateway 错误 (Code: {status_code}): {e}。将返回 RATE_LIMIT 信号。")
                return "RATE_LIMIT"
            else:
                logger.error(f"从 API '进化' Alpha 策略失败: {e}")
            return None