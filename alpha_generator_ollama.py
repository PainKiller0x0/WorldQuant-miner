# --- alpha_generator_ollama.py v7.6.2 (Blacklist Functions & Variables) ---
import argparse
import logging
import json
import os
import time
import requests
import random
from requests.adapters import HTTPAdapter, Retry
from openai import OpenAI
from datetime import datetime
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# --- BUG 修复: 将 logger 定义移至全局作用域 ---
logger = logging.getLogger(__name__)
# --- 修复结束 ---

# --- v7.4 调整: 区分不同 API 的冷却时间 ---
LLM_API_COOLDOWN = 3600  # 1 小时 (针对 LLM API 500/429 错误)
WQ_API_COOLDOWN = 60     # 1 分钟 (针对 WorldQuant 429 错误)
# --- v7.4 结束 ---

# --- v7.6 调整: 黑名单文件及计数 ---
INVALID_FUNCTIONS_FILE = "invalid_functions.json"
BLACKLIST_MAX_STRIKES = 3 # "事不过三"
# --- v7.6 结束 ---


# --- 日志配置 ---
def setup_logging(log_file):
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 清理旧的处理器，避免日志重复
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # --- 基础配置 (INFO及以上，输出到文件和控制台) ---
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(log_dir, log_file)),
                            logging.StreamHandler()
                        ])

    base_name = os.path.splitext(log_file)[0] 
    issue_log_file = f"{base_name}_issues.log" 
    issue_log_path = os.path.join(log_dir, issue_log_file)

    issue_handler = logging.FileHandler(issue_log_path)
    issue_handler.setLevel(logging.WARNING) 
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    issue_handler.setFormatter(formatter)
    
    logging.getLogger('').addHandler(issue_handler)
    
    # 现在 logger 是全局的，可以直接使用
    logger.info("日志系统初始化完成。INFO及以上信息将输出到控制台和主日志文件。")
    logger.info(f"WARNING及以上的问题将额外记录到: {issue_log_path}")

def is_alpha_syntactically_suspicious(alpha_code: str) -> bool:
    # logger 现在是全局的，此函数可以正常工作
    ts_functions_pattern = r'ts_([a-zA-Z_]+)\(([^,)]+)\)'
    match = re.search(ts_functions_pattern, alpha_code)
    if match:
        params = match.group(2).split(',')
        if len(params) == 1 and not params[0].strip().isdigit():
            logger.warning(f"本地预检失败: Alpha '{alpha_code}' 中的函数 '{match.group(0)}' 可能缺少 lookback 参数。已拒绝。")
            return True
    return False

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        self.auth_lock = threading.Lock()
        self._authenticate()
        self.default_settings = {
            'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA',
            'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY',
            'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
            'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False,
        }

    def _create_resilient_session(self):
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('https://', adapter)
        # logger 现在是全局的，此行可以正常工作
        logger.info("创建了带有3次重试机制的API会话。")
        return session

    def _authenticate(self):
        with self.auth_lock:
            url = f"{self.base_url}/authentication"
            try:
                self.session.auth = (self.user_id, self.api_key)
                response = self.session.post(url, timeout=30)
                response.raise_for_status()
                logger.info("WorldQuant Brain authentication successful.")
            except requests.exceptions.RequestException as e:
                # v7.3 修改: 429 错误会在这里被捕获并抛出，由 __main__ 中的启动逻辑处理
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                raise

    def get_data_fields(self):
        # --- v7.5 优化: 扩展数据字段列表 ---
        logger.info("正在使用扩展的、针对高级用户的官方核心数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap", 
            "cap", "returns", "turnover", "beta", "momentum", 
            "adv20", "adv40", "adv60", "adv80", "adv120", 
            "buy_turnover", "sell_turnover", "indneutral_beta"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个核心及高级数据字段。")
        return safe_fields
        # --- v7.5 结束 ---

    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            op_list = data.get('results', []) if isinstance(data, dict) else data
            operators = [str(op) for op in op_list]
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            # v7.3 修改: 检查 429
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"获取操作符时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
            logger.error(f"Failed to get operators: {e}")
            return []

    def test_alpha(self, alpha_expression: str, custom_settings: dict = None):
        submit_url = f"{self.base_url}/simulations"
        current_settings = self.default_settings.copy()
        if custom_settings:
            current_settings.update(custom_settings)
            logger.info(f"使用自定义参数进行测试: {custom_settings}")

        payload = {'type': 'REGULAR', 'regular': alpha_expression, 'settings': current_settings}
        
        try:
            submit_response = self.session.post(submit_url, json=payload, timeout=120)
            if submit_response.status_code == 401:
                logger.warning("提交时认证失败 (401)，正在尝试重新认证...")
                self._authenticate()
                submit_response = self.session.post(submit_url, json=payload, timeout=120)
            submit_response.raise_for_status()
            progress_url = submit_response.headers.get('location')
            if not progress_url:
                logger.error(f"提交模拟任务后，未能从Header获取 location。")
                return None
            logger.info(f"成功提交模拟任务，进度URL: {progress_url}")
        except requests.exceptions.RequestException as e:
            # --- v7.3 修改: 捕获 429 Rate Limit ---
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"提交模拟时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
            # --- v7.3 结束 ---
            error_content = "No response body"
            if e.response is not None:
                try: error_content = e.response.json()
                except json.JSONDecodeError: error_content = e.response.text
            logger.error(f"提交模拟任务失败 '{alpha_expression}': {e} - Response: {error_content}")
            return None

        POLLING_TIMEOUT = 1800
        polling_start_time = time.time()

        while time.time() - polling_start_time < POLLING_TIMEOUT:
            try:
                poll_response = self.session.get(progress_url, timeout=120)
                if poll_response.status_code == 401:
                    logger.warning("轮询时认证失败 (401)，正在尝试重新认证...")
                    self._authenticate()
                    continue
                poll_response.raise_for_status()
                result_data = poll_response.json()
                status = result_data.get("status")

                if status == "COMPLETE":
                    alpha_id = result_data.get("alpha")
                    if not alpha_id:
                        logger.error(f"模拟完成，但未找到 alpha id。")
                        return None
                    final_alpha_url = f"{self.base_url}/alphas/{alpha_id}"
                    final_response = self.session.get(final_alpha_url, timeout=30)
                    final_data = final_response.json()
                    logger.info(f"Alpha '{alpha_id}' 模拟完成。")
                    return final_data
                elif status == "ERROR":
                    logger.error(f"Alpha 模拟出错，服务器返回的完整错误报告: {result_data}")
                    # --- v7.6 修改: 返回完整的错误 JSON ---
                    return result_data 
                    # --- v7.6 结束 ---
                else:
                    logger.debug(f"Alpha '{alpha_expression}' 仍在模拟中... 状态: {status}")
                    time.sleep(10)
            except requests.exceptions.RequestException as e:
                # --- v7.3 修改: 捕获 429 Rate Limit ---
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                    logger.critical(f"轮询结果时检测到 WorldQuant 429 Rate Limit: {e}")
                    return "RATE_LIMIT"
                # --- v7.3 结束 ---
                logger.error(f"轮询结果失败: {e}，将在15秒后重试...")
                time.sleep(15)
            except Exception as e:
                logger.error(f"处理轮询结果时发生未知错误: {e}")
                return None
        
        logger.warning(f"Alpha '{alpha_expression}' 模拟超时（超过 {POLLING_TIMEOUT/60:.0f} 分钟）。")
        return "TIMEOUT"

class AlphaGenerator:
    def __init__(self, wq, api_config_path, batch_size=5):
        self.wq = wq
        self.batch_size = batch_size
        self.model_name = "gemini-2.5-flash-lite"
        logger.info(f"将使用您指定的模型: {self.model_name}")

        try:
            with open(api_config_path, 'r') as f: config = json.load(f)
            self.client = OpenAI(api_key=config.get('api_key', 'painkiller0x0'), base_url=config['base_url'])
            logger.info(f"API client initialized for endpoint: {config['base_url']}")
        except Exception as e:
            logger.critical(f"加载 API 配置或初始化客户端失败: {e}")
            raise
        
        self.hopeful_alphas_file = "hopeful_alphas.json"
        self.tested_alphas_logfile = "tested_alphas_log.json"
        self.purged_alphas_archive_file = "purged_alphas_archive.json"
        self.tested_alphas = self.load_tested_alphas()
        self.hopeful_alphas_cache = []
        
        # --- v7.4 调整: 冷却状态 ---
        self.llm_api_cooldown = LLM_API_COOLDOWN
        self.wq_api_cooldown = WQ_API_COOLDOWN
        self._rate_limit_until = 0
        # --- v7.4 结束 ---
        
        # --- v7.6.2 调整: "事不过三"标识符黑名单 ---
        self.invalid_functions_file = INVALID_FUNCTIONS_FILE
        self.blacklist_lock = threading.Lock()
        self.blacklist_counts = self.load_blacklist_counts() 
        self.blacklist_max_strikes = BLACKLIST_MAX_STRIKES
        # v7.6.2: 使用 \b (单词边界) 来匹配所有独立标识符 (变量或函数名)
        self.identifier_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z_0-9]*)\b')
        self.fields = [] # 用于存储字段列表
        # --- v7.6.2 结束 ---

    # --- v7.4 调整: 冷却触发器接受时长 ---
    def _enter_cooldown(self, duration_seconds, reason="Rate Limit"):
        """触发冷却期"""
        self._rate_limit_until = time.time() + duration_seconds
        duration_minutes = duration_seconds / 60
        logger.warning(f"检测到 {reason}。脚本将进入冷却期 {duration_minutes:.0f} 分钟，直到 {datetime.fromtimestamp(self._rate_limit_until).strftime('%Y-%m-%d %H:%M:%S')}")
    # --- v7.4 结束 ---

    def load_tested_alphas(self):
        if not os.path.exists(self.tested_alphas_logfile): return set()
        try:
            with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return set()
                data = json.loads(content)
                return set(item.get('expression') for item in data if item.get('expression'))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"加载 {self.tested_alphas_logfile} 出错: {e}, 将创建一个新的记录文件。")
            return set()
            
    # --- v7.6.1 调整: 加载黑名单计数 ---
    def load_blacklist_counts(self):
        with self.blacklist_lock:
            if not os.path.exists(self.invalid_functions_file):
                logger.info("无效标识符计数文件(invalid_functions.json)不存在，将创建新的。")
                return {} # 返回空字典
            try:
                with open(self.invalid_functions_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content: 
                        return {} # 空文件，返回空字典
                    
                    data = json.loads(content)
                    
                    if not isinstance(data, dict):
                        logger.warning(f"{self.invalid_functions_file} 格式不正确 (不是字典)，将重置。")
                        return {}
                        
                    logger.info(f"成功加载 {len(data)} 个标识符的黑名单计数。")
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 {self.invalid_functions_file} 出错: {e}, 将创建新的。")
                return {} # 出错，返回空字典
    
    # --- v7.6.1 调整: 更新黑名单计数 ---
    def update_blacklist_count(self, identifier_name):
        with self.blacklist_lock:
            # 再次从文件加载，确保多线程安全和数据最新
            current_counts = self.load_blacklist_counts()
            
            current_count = current_counts.get(identifier_name, 0)
            current_count += 1
            current_counts[identifier_name] = current_count
            
            try:
                with open(self.invalid_functions_file, 'w', encoding='utf-8') as f:
                    json.dump(current_counts, f, indent=4)
                
                # 同步更新内存中的计数
                self.blacklist_counts = current_counts
                
                if current_count < self.blacklist_max_strikes:
                    logger.warning(f"检测到无效标识符: '{identifier_name}'。计数: {current_count}/{self.blacklist_max_strikes}。")
                else:
                    logger.critical(f"'{identifier_name}' 已达到 {current_count}/{self.blacklist_max_strikes} 次计数，将被永久拉黑。")
                    
            except IOError as e:
                logger.error(f"保存黑名单计数文件时出错: {e}")
                
    # --- v7.6.2 调整: 检查是否被拉黑 (原 is_using_blacklisted_function) ---
    def is_using_blacklisted_identifier(self, alpha_code: str) -> bool:
        if not self.blacklist_counts:
            return False # 黑名单为空，跳过检查
        
        # v7.6.2: 使用新的 identifier_pattern
        found_identifiers = self.identifier_pattern.findall(alpha_code)
        if not found_identifiers:
            return False
            
        for identifier in found_identifiers:
            # 检查标识符是否在计数器中，并且计数是否达到阈值
            if identifier in self.blacklist_counts and self.blacklist_counts[identifier] >= self.blacklist_max_strikes:
                logger.warning(f"预检拦截: Alpha '{alpha_code}' 包含了已被拉黑的标识符 '{identifier}' (计数: {self.blacklist_counts[identifier]}/{self.blacklist_max_strikes})。")
                return True
        return False
    # --- v7.6.2 结束 ---

    def excavate_one_pearl(self, sample_size=200):
        if not os.path.exists(self.tested_alphas_logfile):
            return None

        try:
            with open(self.tested_alphas_logfile, 'r') as f:
                all_tested = json.load(f)
        except (IOError, json.JSONDecodeError):
            logger.error(f"考古挖掘失败：无法读取 {self.tested_alphas_logfile}")
            return None

        if len(all_tested) > sample_size:
            sample_records = random.sample(all_tested, sample_size)
        else:
            sample_records = all_tested

        hopeful_expressions = {alpha.get('expression') for alpha in self.hopeful_alphas_cache}

        potential_pearls = []
        for record in sample_records:
            if record.get('status') != 'COMPLETE' or record.get('expression') in hopeful_expressions:
                continue
            
            passed_count = record.get('passed_checks', 0)
            fitness = record.get('fitness', -999)

            if passed_count == 3 and fitness > -1.0:
                record['potential_score'] = self._calculate_potential_score(record)
                potential_pearls.append(record)

        if not potential_pearls:
            return None

        potential_pearls.sort(key=lambda x: x.get('potential_score', -999), reverse=True)
        best_pearl = potential_pearls[0]
        logger.info(f"考古学家在 {len(sample_records)} 条记录中发现一颗遗珠！潜力分: {best_pearl['potential_score']:.3f}, Expression: {best_pearl['expression']}")
        return {"expression": best_pearl['expression'], "performance": best_pearl.get('performance', {})}
        
    def _calculate_potential_score(self, record):
        try:
            fitness = float(record.get('fitness', -999))
            passed_count = int(record.get('passed_checks', 0))
            performance = record.get('performance', {})
            sharpe = float(performance.get('sharpe', 0.0))
            turnover = float(performance.get('turnover', 1.0))
            score = fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)
            return score
        except (ValueError, TypeError):
            return -999

    def load_evolution_seeds(self, sample_size=20):
        seeds = []
        if os.path.exists(self.hopeful_alphas_file):
            try:
                with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content:
                        self.hopeful_alphas_cache = json.loads(content)
                        seeds.extend(self.hopeful_alphas_cache)
            except (IOError, json.JSONDecodeError):
                logger.error(f"加载精英池 {self.hopeful_alphas_file} 失败。")
        
        logger.info(f"已加载 {len(self.hopeful_alphas_cache)} 个精英策略。")
        
        pearl = self.excavate_one_pearl()
        if pearl:
            seeds.append(pearl)
        
        if not seeds:
            logger.warning("精英池为空，且未挖掘到遗珠，无法获取进化种子。")
            return []

        final_sample_size = min(sample_size, len(seeds))
        evolution_seeds = random.sample(seeds, final_sample_size)
        
        logger.info(f"策略导师将从 {len(self.hopeful_alphas_cache)} 个精英策略中学习模式。")
        logger.info(f"已从总池（含遗珠）中随机抽取 {len(evolution_seeds)} 个作为本轮进化种子。")
        return evolution_seeds

    def analyze_successful_patterns(self, top_k=5):
        if not self.hopeful_alphas_cache:
            return []
        all_expressions = [alpha.get('expression', '') for alpha in self.hopeful_alphas_cache]
        operator_pattern = re.compile(r'([a-zA-Z_0-9]+)\s*\(')
        all_operators = []
        for expr in all_expressions:
            if expr:
                operators_in_expr = operator_pattern.findall(expr)
                all_operators.extend(operators_in_expr)
        if not all_operators:
            return []
        most_common = [op for op, count in Counter(all_operators).most_common(top_k)]
        logger.info(f"策略导师分析完成: 发现最常见的 {top_k} 个成功模式是 {most_common}")
        return most_common

    def generate_alpha_idea(self, fields, operators, guidance=None):
        field_list = ", ".join(fields)
        
        # --- v7.5 优化: 动态混合操作符 ---
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']
        
        # 从完整列表中排除核心操作符，然后随机抽取
        advanced_operators = [op for op in operators if op not in core_operators]
        sample_size = min(len(advanced_operators), 20) # 最多抽取20个
        extra_operators = random.sample(advanced_operators, sample_size)
        
        combined_operators = core_operators + extra_operators
        operator_list = ", ".join(combined_operators)
        logger.info(f"本轮 Discover 将使用 {len(combined_operators)} 个操作符 (13 核心 + {sample_size} 随机)。")
        # --- v7.5 结束 ---
        
        prompt_lines = [
            "You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.",
            "Follow these rules strictly:",
            "1.  **Use ONLY the provided fields and operators.**",
            "2.  **The expression MUST end with a semicolon (;).**",
            "3.  **IMPORTANT SYNTAX:** All functions starting with `ts_` (like `ts_corr`, `ts_mean`, etc.) MUST have a second integer argument for the lookback period (e.g., `ts_mean(close, 10)`).",
            # --- v7.5 优化: 放宽复杂度 ---
            "4.  **Complexity:** Try to keep operators below 15, but more complex and creative combinations are encouraged.",
            "5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression.",
            "6.  **Be Creative:** Do not just combine `close` and `vwap`. Use other fields like `cap`, `adv20`, or `returns`."
            # --- v7.5 结束 ---
        ]
        
        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Our analysis shows that expressions using `{', '.join(guidance)}` tend to be more successful. Try to incorporate these patterns.")

        prompt_lines.extend([
            f"**Available Data Fields:** {field_list}",
            f"**Allowed Operators:** {operator_list}", # v7.5
            "New Alpha Expression:"
        ])
        prompt = "\n".join(prompt_lines)

        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=100, temperature=0.9)
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
            if idea and not idea.endswith(';'): idea += ';'
            return {"expression": idea, "settings": {}}
        except Exception as e:
            # --- v7.3 修改: 捕获 LLM API 错误 (500 或 429) ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code == 500:
                logger.warning(f"生成 Alpha 时检测到 LLM Gateway 的 HTTP 500 错误: {e}。将其视为 Rate Limit 信号。")
                self._enter_cooldown(self.llm_api_cooldown, reason="LLM Gateway 500 Error") # v7.4
            elif status_code == 429:
                logger.warning(f"生成 Alpha 时检测到 LLM API 429 Rate Limit: {e}。")
                self._enter_cooldown(self.llm_api_cooldown, reason="LLM 429 Rate Limit") # v7.4
            else:
                logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None
            # --- v7.3 结束 ---

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        base_expression = base_alpha_obj.get('expression')
        base_settings = base_alpha_obj.get('performance', {}).get('settings', self.wq.default_settings)

        prompt_lines = [
            "You are an AI machine that generates code. Your SOLE task is to evolve a given investment strategy for WorldQuant.",
            "You MUST output ONLY a single, valid JSON object in a markdown code block. Do NOT include any explanations, analysis, or introductory text.",
            f"**Base Strategy for Evolution:**",
            f"- Expression: `{base_expression}`",
            f"- Settings: `{json.dumps(base_settings)}`"
        ]

        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Analysis suggests these patterns are successful: `{', '.join(guidance)}`. Your evolution should try to incorporate one of these patterns.")
        
        prompt_lines.extend([
            "\n**Task:** Apply ONE of the following evolution strategies:",
            # --- v7.5 优化: 放宽复杂度 ---
            "1.  **Evolve Expression:** Make a small, creative change to the expression. Prioritize using the strategic guidance if available. Keep the expression concise (under 15 operators if possible). Feel free to introduce new fields or operators.",
            "2.  **Evolve Settings:** Make a small, logical change to ONE numeric setting (`delay`, `decay`, `truncation`).",
            # --- v7.5 结束 ---
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
                evolved_strategy = json.loads(json_str)

            if 'expression' not in evolved_strategy or 'settings' not in evolved_strategy:
                logger.error("进化返回的JSON格式无效，缺少expression或settings键。")
                return None
            return evolved_strategy
        except Exception as e:
            # --- v7.3 修改: 捕获 LLM API 错误 (500 或 429) ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code == 500:
                logger.warning(f"进化 Alpha 时检测到 LLM Gateway 的 HTTP 500 错误: {e}。将其视为 Rate Limit 信号。")
                self._enter_cooldown(self.llm_api_cooldown, reason="LLM Gateway 500 Error") # v7.4
            elif status_code == 429:
                logger.warning(f"进化 Alpha 时检测到 LLM API 429 Rate Limit: {e}。")
                self._enter_cooldown(self.llm_api_cooldown, reason="LLM 429 Rate Limit") # v7.4
            else:
                logger.error(f"从 API '进化' Alpha 策略失败: {e}")
            return None
            # --- v7.3 结束 ---

    def log_tested_alphas(self, reports_to_log):
        all_reports = []
        if os.path.exists(self.tested_alphas_logfile):
            try:
                with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: all_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {self.tested_alphas_logfile}，将创建新的日志文件。")

        all_reports.extend(reports_to_log)
        for report in reports_to_log:
            if 'expression' in report: self.tested_alphas.add(report['expression'])
        
        try:
            with open(self.tested_alphas_logfile, 'w', encoding='utf-8') as f: json.dump(all_reports, f, indent=4, ensure_ascii=False)
        except IOError as e: logger.error(f"写入全量日志文件时出错: {e}")

    def archive_purged_alphas(self, purged_reports, reason="淘汰"):
        if not purged_reports:
            return
        
        all_archived = []
        if os.path.exists(self.purged_alphas_archive_file):
            try:
                with open(self.purged_alphas_archive_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: all_archived = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析归档文件 {self.purged_alphas_archive_file}，将创建新文件。")
        
        for report in purged_reports:
            report['archive_reason'] = reason
            report['archive_timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        all_archived.extend(purged_reports)
        try:
            with open(self.purged_alphas_archive_file, 'w', encoding='utf-8') as f: 
                json.dump(all_archived, f, indent=4)
            logger.info(f"已将 {len(purged_reports)} 个被淘汰的策略 ({reason}) 存入归档文件。")
        except IOError as e:
            logger.error(f"写入归档文件时出错: {e}")
    
    def save_hopeful_reports(self, new_hopeful_reports, max_pool_size=200):
        existing_reports = []
        if os.path.exists(self.hopeful_alphas_file):
            try:
                with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: existing_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {self.hopeful_alphas_file}，将创建新的精华文件。")

        combined_reports = existing_reports + new_hopeful_reports
        
        purged_reports = []
        archived_reports = []
        
        unique_reports_map = {report.get('expression'): report for report in combined_reports}
        
        for report in unique_reports_map.values():
            fitness = report.get('performance', {}).get('fitness', -999)
            checks_summary = report.get('checks_summary', '0 PASS')
            try:
                passed_count = int(checks_summary.split(' ')[0])
            except (ValueError, IndexError):
                passed_count = 0

            is_high_quality = fitness > 0 and passed_count >= 4
            is_high_potential = fitness > -0.5 and passed_count >= 5
            
            if is_high_quality or is_high_potential:
                purged_reports.append(report)
            elif any(r['expression'] == report['expression'] for r in existing_reports):
                archived_reports.append(report)

        logger.info(f"精英池清洗: {len(unique_reports_map)} -> {len(purged_reports)} (识别出 {len(archived_reports)} 个过时策略)")
        
        self.archive_purged_alphas(archived_reports, reason="标准清洗")

        def calculate_combined_score(report):
            fitness = report.get('performance', {}).get('fitness', -999)
            sharpe = report.get('performance', {}).get('sharpe', 0.0)
            turnover = report.get('performance', {}).get('turnover', 1.0)
            checks_summary = report.get('checks_summary', '0 PASS')
            try: passed_count = int(checks_summary.split(' ')[0])
            except (ValueError, IndexError): passed_count = 0
            
            score = fitness + (passed_count * 0.2) + (abs(sharpe) * 0.3) - (turnover * 0.1)
            return score

        purged_reports.sort(key=calculate_combined_score, reverse=True)
        
        final_pool = purged_reports[:max_pool_size]
        
        if len(purged_reports) > max_pool_size:
            eliminated = purged_reports[max_pool_size:]
            logger.info(f"精英池末位淘汰: {len(purged_reports)} -> {len(final_pool)} (保留综合评分排名前 {max_pool_size} 的策略)")
            self.archive_purged_alphas(eliminated, reason="末位淘汰")

        try:
            with open(self.hopeful_alphas_file, 'w', encoding='utf-8') as f: 
                json.dump(final_pool, f, indent=4, ensure_ascii=False)
            logger.info(f"已将 {len(new_hopeful_reports)} 份新战报处理完毕，并完成了精英池的动态维护。当前池中共有 {len(final_pool)} 个策略。")
        except IOError as e: 
            logger.error(f"保存精华战报文件时出错: {e}")

    def run(self, mode='discover', concurrency_level=2, sleep_time=10):
        is_first_run = True
        
        logger.info(f"Alpha 生成器启动 | 模式: {mode.upper()} | 并发等级: {concurrency_level} | 轮间间隔: {sleep_time}s")
        # --- v7.6 修改: 将 fields 和 operators 存为实例属性 ---
        self.fields = self.wq.get_data_fields()
        self.operators = self.wq.get_operators()
        # --- v7.6 结束 ---
        
        # v7.3 修改: 检查 get_operators 是否返回了 Rate Limit 信号
        if self.operators == "RATE_LIMIT":
            logger.critical("获取操作符时遭遇 WorldQuant 429，触发冷却。")
            self._enter_cooldown(self.wq_api_cooldown, reason="WorldQuant 429 Rate Limit") # v7.4
        
        if not self.fields or not self.operators or self.operators == "RATE_LIMIT":
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            if self.operators != "RATE_LIMIT": # 如果不是因为Rate Limit，就睡60s退出
                time.sleep(60)
            # 如果是Rate Limit，run 循环会处理冷却
        
        evolution_seeds = []
        strategic_guidance = []
        if mode == 'evolve':
            evolution_seeds = self.load_evolution_seeds(sample_size=20) 
            if not evolution_seeds:
                mode = 'discover'
                logger.warning("进化模式无法启动（无可用种子），已自动切换到发现模式。")
            else:
                strategic_guidance = self.analyze_successful_patterns()

        while True:
            # --- v7.3 新增: 检查冷却状态 ---
            if time.time() < self._rate_limit_until:
                remaining = self._rate_limit_until - time.time()
                logger.info(f"当前处于冷却期。将在 {remaining/60:.1f} 分钟后恢复... (冷却至 {datetime.fromtimestamp(self._rate_limit_until).strftime('%Y-%m-%d %H:%M:%S')})")
                # 睡5分钟或剩余时间
                time.sleep(min(remaining, 300)) 
                continue # 跳过本轮循环
            # --- v7.3 结束 ---

            # v7.3 修改: 确保 fields 和 operators 正常
            if not self.fields or not self.operators or self.operators == "RATE_LIMIT":
                logger.warning("Fields 或 Operators 未就绪，正在尝试重新获取...")
                self.fields = self.wq.get_data_fields()
                self.operators = self.wq.get_operators()
                if self.operators == "RATE_LIMIT":
                    logger.critical("获取操作符时遭遇 WorldQuant 429，触发冷却。")
                    self._enter_cooldown(self.wq_api_cooldown, reason="WorldQuant 429 Rate Limit") # v7.4
                    continue
                if not self.fields or not self.operators:
                    logger.error("仍然无法获取字段或操作符，将在60秒后重试。")
                    time.sleep(60)
                    continue

            current_batch_size = 1 if is_first_run else self.batch_size
            current_concurrency = 1 if is_first_run else concurrency_level

            if is_first_run:
                logger.info("***** 首次运行，进入安全模式 (batch=1, concurrency=1) *****")

            logger.info(f"[{mode.upper()}] 开始新一轮 Alpha 生成，目标数量: {current_batch_size}")
            
            strategies_to_test = []
            if mode == 'discover':
                for _ in range(current_batch_size):
                    idea = self.generate_alpha_idea(self.fields, self.operators, guidance=strategic_guidance)
                    if idea: strategies_to_test.append(idea)
            elif mode == 'evolve':
                for _ in range(current_batch_size):
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance)
                    if idea: strategies_to_test.append(idea)

            # --- v7.6.2 修改: 预检逻辑 ---
            pre_valid_strategies = [s for s in strategies_to_test if s and s.get("expression") and s.get("expression") not in self.tested_alphas]
            
            valid_strategies = []
            for s in pre_valid_strategies:
                expr = s.get("expression")
                if is_alpha_syntactically_suspicious(expr):
                    continue
                # v7.6.2: 使用新的 "标识符" 检查
                if self.is_using_blacklisted_identifier(expr): 
                    continue
                valid_strategies.append(s)
            # --- v7.6.2 结束 ---

            logger.info(f"成功生成 {len(valid_strategies)} 个通过预检且待测试的新策略。")
            
            if valid_strategies:
                new_hopeful_reports = []
                reports_to_log = []
                logger.info(f"开始并行测试 {len(valid_strategies)} 个新策略，并发数: {current_concurrency}...")
                
                with ThreadPoolExecutor(max_workers=current_concurrency) as executor:
                    future_to_strategy = {executor.submit(self.wq.test_alpha, s['expression'], s['settings']): s for s in valid_strategies}
                    for future in as_completed(future_to_strategy):
                        strategy = future_to_strategy[future]
                        idea_expr = strategy['expression']
                        log_report = {"expression": idea_expr, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        try:
                            result = future.result()

                            # --- v7.3 新增: 处理来自 WQ 模拟的 Rate Limit 信号 ---
                            if result == "RATE_LIMIT":
                                logger.critical(f"WorldQuant 模拟返回 'RATE_LIMIT' 信号 (针对: {idea_expr})。")
                                self._enter_cooldown(self.wq_api_cooldown, reason="WorldQuant 429 Rate Limit") # v7.4
                                continue
                            # --- v7.3 结束 ---
                            
                            # --- v7.6.2: 扩展黑名单逻辑 (捕获函数、操作符和变量) ---
                            if isinstance(result, dict) and result.get("status") == "ERROR":
                                log_report["status"] = "ERROR"
                                reports_to_log.append(log_report)
                                
                                # 检查 WQ 返回的详细错误信息
                                error_message = ""
                                regular_errors = result.get("regular", {}).get("errors", [])
                                if regular_errors and isinstance(regular_errors, list) and len(regular_errors) > 0:
                                    error_message = regular_errors[0].get("message", "")
                                else:
                                    error_message = result.get("message", "") # Fallback

                                # v7.6.2: 扩展 regex 以捕获 'unknown variable'
                                match = re.search(r"(Unknown function|unknown operator|unknown variable) '(\w+)'", error_message)
                                
                                if match:
                                    error_type = match.group(1) # "Unknown function", "unknown variable", etc.
                                    bad_identifier = match.group(2) # "adv40", "vwma", etc.

                                    # v7.6.2: 改进的黑名单逻辑
                                    # 1. 如果是 "unknown variable" (如 adv40)，WQ 认为它无效，直接拉黑 (无视 self.fields)。
                                    # 2. 如果是 "Unknown function" (如 adv40())，但 adv40 在 self.fields 中，
                                    #    说明它是被误用为函数的 *字段*，此时不应拉黑该 *字段*。
                                    
                                    should_blacklist = False
                                    if error_type == "unknown variable":
                                        should_blacklist = True
                                        logger.warning(f"检测到无效变量: '{bad_identifier}'。WQ API 报告其未知。")
                                    elif error_type in ["Unknown function", "unknown operator"]:
                                        if bad_identifier not in self.fields:
                                            should_blacklist = True
                                            logger.warning(f"检测到无效函数/操作符: '{bad_identifier}'。")
                                        else:
                                            # 这是 v7.6.1 的 "误用" 逻辑，是正确的
                                            logger.info(f"Alpha 模拟出错: '{bad_identifier}' 是一个数据字段，但被误用为函数。已记录，不计入黑名单。")
                                    
                                    if should_blacklist:
                                        self.update_blacklist_count(bad_identifier) # Add to blacklist
                                    
                                else:
                                    # v7.6.1: 其他错误，不触发黑名单
                                    logger.warning(f"Alpha 模拟出错 (非特定标识符错误)，已记录: {idea_expr} | Error: {error_message[:200]}...")
                                continue
                            # --- v7.6.2 结束 ---

                            if result in ["TIMEOUT"]: # "ERROR" 已被上面的 dict 捕获
                                log_report["status"] = result
                                reports_to_log.append(log_report)
                                # v7.6.1: TIMEOUT 不触发黑名单
                                logger.warning(f"Alpha 模拟{result}，已记录并丢弃 (不计入黑名单): {idea_expr}")
                                continue
                            
                            if result: # 此时 result 必然是 COMPLETE 的成功 JSON
                                is_stats = result.get("is", {})
                                alpha_id = result.get("id")
                                if not is_stats or not alpha_id: continue
                                
                                checks = result.get("is", {}).get("checks", [])
                                passed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PASS")
                                fitness = is_stats.get('fitness', -999)

                                log_report["status"] = "COMPLETE"
                                log_report["fitness"] = fitness
                                log_report["passed_checks"] = passed_count
                                log_report["performance"] = is_stats

                                failed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "FAIL")
                                pending_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PENDING")
                                checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                                is_high_quality = fitness > 0 and passed_count >= 4
                                is_high_potential = fitness > -0.5 and passed_count >= 5

                                if is_high_quality or is_high_potential:
                                    if is_high_potential and not is_high_quality:
                                        logger.info(f"发现一个高潜力策略 (Fitness < 0, 但 Checks >= 5)，破格录用！ Fitness: {fitness:.3f}, Checks: {passed_count} PASS. Alpha: {idea_expr}")
                                    else:
                                        logger.info(f"发现一个高质量策略！ Fitness: {fitness:.3f}, Checks: {passed_count} PASS. Alpha: {idea_expr}")
                                    
                                    hopeful_report = {
                                        "expression": result.get("regular", {}).get("code"),
                                        "alpha_id": alpha_id,
                                        "result_url": f"https://platform.worldquantbrain.com/alphas/regular/{alpha_id}",
                                        "grade": result.get("grade", "UNKNOWN"),
                                        "timestamp": log_report["timestamp"],
                                        "performance": is_stats,
                                        "checks_summary": checks_summary
                                    }
                                    perf_items = is_stats.items()
                                    stats_str = ", ".join([f"{key}: {value:.3f}" for key, value in perf_items if isinstance(value, (int, float))])
                                    logger.info(f"生成高质量策略战报 [{checks_summary}] -> {stats_str}")
                                    new_hopeful_reports.append(hopeful_report)
                                else:
                                    logger.info(f"策略未达到高质量标准，已丢弃。Fitness: {fitness:.3f}, Checks: {passed_count} PASS. Alpha: {idea_expr}")

                        except Exception as exc:
                            logger.error(f"处理策略 '{idea_expr}' 的结果时发生意外错误: {exc}", exc_info=True)
                            log_report["status"] = "EXCEPTION"
                            reports_to_log.append(log_report)
                
                if reports_to_log:
                    self.log_tested_alphas(reports_to_log)
                    logger.info(f"已将 {len(reports_to_log)} 条测试记录更新到 {self.tested_alphas_logfile}")
                
                if new_hopeful_reports:
                    self.save_hopeful_reports(new_hopeful_reports)
                else:
                    logger.info("本轮所有策略均未达到高质量标准，未更新精华战报文件。")

            if mode == 'evolve':
                evolution_seeds = self.load_evolution_seeds(sample_size=20)
                if not evolution_seeds:
                    mode = 'discover'
                    logger.warning("进化模式无法启动（无可用种子），已自动切换到发现模式。")
                else:
                    strategic_guidance = self.analyze_successful_patterns()

            if is_first_run:
                logger.info("***** 安全模式运行结束，下轮将恢复正常 *****")
                is_first_run = False

            logger.info(f"本轮结束。等待{sleep_time}秒开始下一轮...")
            time.sleep(sleep_time)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator using a generic API endpoint')
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    parser.add_argument('--concurrency', type=int, default=2, help="Number of alphas to test concurrently")
    parser.add_argument('--sleep', type=int, default=10, help="Seconds to wait between generation cycles")
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve'], help="Generation mode")
    parser.add_argument('--log-file', type=str, default='alpha_generator.log', help="Name of the log file in the logs directory")
    args = parser.parse_args()

    setup_logging(args.log_file)

    MAX_INIT_RETRIES = 5
    SHORT_SLEEP = 30
    LONG_SLEEP = 300

    retry_count = 0
    wq_client = None

    while wq_client is None:
        try:
            wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
            logger.info("WorldQuant 客户端初始化成功。")
            retry_count = 0
        except requests.exceptions.RequestException as e:
            # --- v7.3 修改: 捕获 WQ 初始化时的 429 错误 ---
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"初始化 WorldQuant 客户端时检测到 429 Rate Limit: {e}。")
                # --- v7.4 调整: 使用 WQ 专属冷却时间 ---
                cooldown_end = time.time() + WQ_API_COOLDOWN 
                logger.warning(f"将进入 {WQ_API_COOLDOWN/60:.0f} 分钟冷却期，直到 {datetime.fromtimestamp(cooldown_end).strftime('%Y-%m-%d %H:%M:%S')}")
                
                while time.time() < cooldown_end:
                    remaining = cooldown_end - time.time()
                    logger.info(f"初始化冷却中... {remaining:.0f} 秒后重试。")
                    time.sleep(min(remaining, WQ_API_COOLDOWN)) # 睡 1 分钟或剩余时间
                # --- v7.4 结束 ---
                
                retry_count = 0 # 重置重试次数
                continue # 返回循环顶部，再次尝试初始化
            # --- v7.3 结束 ---

            logger.error(f"初始化 WorldQuant 客户端失败: {e}")
            retry_count += 1
            if retry_count <= MAX_INIT_RETRIES:
                logger.info(f"将在 {SHORT_SLEEP} 秒后重试... (尝试次数 {retry_count}/{MAX_INIT_RETRIES})")
                time.sleep(SHORT_SLEEP)
            else:
                logger.warning(f"已达到最大初始重试次数。将在 {LONG_SLEEP/60:.0f} 分钟后再次尝试...")
                time.sleep(LONG_SLEEP)
                retry_count = 0
    
    try:
        generator = AlphaGenerator(wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size)
        generator.run(mode=args.mode, concurrency_level=args.concurrency, sleep_time=args.sleep)
    except Exception as e:
        logger.critical(f"生成器运行时发生致命错误: {e}", exc_info=True)