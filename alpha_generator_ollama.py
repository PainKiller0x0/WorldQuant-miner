# --- alpha_generator_ollama.py v8.0.0 (Control Panel Integration) ---
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
import queue # v7.7 新增

SYSTEM_CONFIG_FILE = "system_config.json" # v8.0

CURRENT_GENERATOR_VERSION = "v8.0.0" # v8.0.0: 集成控制面板配置

# --- v8.0: 辅助函数，用于读取中心化配置 ---
def load_system_config():
    """
    读取并返回 system_config.json 的内容。
    注意: 这会在每次需要时都读取文件，以获取动态参数。
    """
    try:
        with open(SYSTEM_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        # 紧急回退 (Fallback)
        logger.error(f"读取 {SYSTEM_CONFIG_FILE} 失败: {e}。将使用紧急回退值！")
        return {
            "wq_api_cooldown": 30,
            "llm_api_cooldown": 3600,
            "miner_concurrency": 1,
            "miner_sleep": 120,
            "evolver_concurrency": 1,
            "evolver_sleep": 120
        }
# --- v8.0 结束 ---

# --- BUG 修复: 将 logger 定义移至全局作用域 ---
logger = logging.getLogger(__name__)
# --- 修复结束 ---

# --- v8.0: 移除硬编码的 Cooldowns ---
# LLM_API_COOLDOWN = 3600  # <--- v8.0 移除
# WQ_API_COOLDOWN = 30     # <--- v8.0 移除
# --- v8.0 结束 ---

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
                        format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s', # v7.7: 添加 threadName
                        handlers=[
                            logging.FileHandler(os.path.join(log_dir, log_file)),
                            logging.StreamHandler()
                        ])

    base_name = os.path.splitext(log_file)[0]
    issue_log_file = f"{base_name}_issues.log"
    issue_log_path = os.path.join(log_dir, issue_log_file)

    issue_handler = logging.FileHandler(issue_log_path)
    issue_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s') # v7.7: 添加 threadName
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
    # v7.7: __init__ 签名改变，增加了 concurrency_level
    def __init__(self, wq, api_config_path, batch_size=5, concurrency_level=2):
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

        # --- v7.7 新增: 线程安全锁 ---
        self.tested_alphas_lock = threading.Lock()   # 保护 tested_alphas_log.json 和 self.tested_alphas
        self.hopeful_file_lock = threading.Lock()    # 保护 hopeful_alphas.json 和 self.hopeful_alphas_cache
        self.blacklist_lock = threading.Lock()       # v7.6.1 移动到这里，保护 invalid_functions.json
        # --- v7.7 结束 ---

        self.tested_alphas = self.load_tested_alphas() # 已受 load_tested_alphas 内部的锁保护
        self.hopeful_alphas_cache = [] # 将由 load_evolution_seeds 填充 (已加锁)

        # --- v7.4 调整: 冷却状态 ---
        # --- v8.0: 移除硬编码，改为从 _enter_cooldown 动态读取 ---
        # self.llm_api_cooldown = LLM_API_COOLDOWN # <--- v8.0 移除
        # self.wq_api_cooldown = WQ_API_COOLDOWN   # <--- v8.0 移除
        self._rate_limit_until = 0
        # --- v8.0 结束 ---

        # --- v7.6.2 调整: "事不过三"标识符黑名单 ---
        self.invalid_functions_file = INVALID_FUNCTIONS_FILE
        self.blacklist_counts = self.load_blacklist_counts() # 已受 load_blacklist_counts 内部的锁保护
        self.blacklist_max_strikes = BLACKLIST_MAX_STRIKES
        self.identifier_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z_0-9]*)\b')
        self.fields = [] # 用于存储字段列表
        # --- v7.6.2 结束 ---

        # --- v7.7 新增: 生产者-消费者队列 ---
        self.concurrency_level = concurrency_level
        self.queue_max_size = self.concurrency_level * 2 # 队列缓冲区大小
        self.strategy_queue = queue.Queue(maxsize=self.queue_max_size)
        self.consumer_threads = []
        # --- v7.7 结束 ---

    # --- v7.4 调整: 冷却触发器接受时长 ---
    # --- v8.0: 修改为动态读取配置 ---
    def _enter_cooldown(self, reason="Rate Limit"):
        """
        触发冷却期 (v8.0: 动态从 system_config.json 读取时长)
        """
        # 动态读取配置
        config = load_system_config()
        duration_seconds = 3600 # 默认回退
        
        if reason == "WorldQuant 429 Rate Limit":
            duration_seconds = config.get("wq_api_cooldown", 30)
        elif reason in ["LLM Gateway 500 Error", "LLM 429 Rate Limit"]:
            duration_seconds = config.get("llm_api_cooldown", 3600)
        else:
            # 其他未知原因，使用 WQ 冷却
            duration_seconds = config.get("wq_api_cooldown", 60)

        self._rate_limit_until = time.time() + duration_seconds
        duration_minutes = duration_seconds / 60
        logger.warning(f"检测到 {reason}。脚本将进入冷却期 {duration_minutes:.0f} 分钟 ({duration_seconds} 秒)，直到 {datetime.fromtimestamp(self._rate_limit_until).strftime('%Y-%m-%d %H:%M:%S')}")
    # --- v8.0 结束 ---

    def load_tested_alphas(self):
        # v7.7: 增加线程锁
        with self.tested_alphas_lock:
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
        # v7.7: 使用 self.blacklist_lock (之前 v7.6.1/2 是在函数内部定义的锁)
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
    # --- v7.7.1 修复: 确保 load_blacklist_counts 在锁内部被调用以获取最新数据 ---
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

    # --- v7.6.2 调整: 检查是否被拉黑 ---
    def is_using_blacklisted_identifier(self, alpha_code: str) -> bool:
        # v7.7: 读取 self.blacklist_counts 是线程安全的，因为它只在 update_blacklist_count (已加锁) 中被写入
        # 但为了绝对安全，我们锁住读取 (尽管 GIL 可能使其安全，但显式锁更健壮)
        with self.blacklist_lock:
            current_counts = self.blacklist_counts

        if not current_counts:
            return False # 黑名单为空，跳过检查

        found_identifiers = self.identifier_pattern.findall(alpha_code)
        if not found_identifiers:
            return False

        for identifier in found_identifiers:
            if identifier in current_counts and current_counts[identifier] >= self.blacklist_max_strikes:
                logger.warning(f"预检拦截: Alpha '{alpha_code}' 包含了已被拉黑的标识符 '{identifier}' (计数: {current_counts[identifier]}/{self.blacklist_max_strikes})。")
                return True
        return False
    # --- v7.6.2 结束 ---

    def excavate_one_pearl(self, sample_size=200):
        # v7.7: 需要加锁读取 tested_alphas_logfile
        all_tested = []
        if not os.path.exists(self.tested_alphas_logfile):
            return None

        try:
            # v7.7: 加锁
            with self.tested_alphas_lock:
                # v7.8.1: 修复文件为空时的处理
                if os.path.getsize(self.tested_alphas_logfile) < 2:
                     logger.info(f"考古挖掘：{self.tested_alphas_logfile} 文件为空。")
                     return None
                with open(self.tested_alphas_logfile, 'r') as f:
                    all_tested = json.load(f)
        except (IOError, json.JSONDecodeError):
            logger.error(f"考古挖掘失败：无法读取 {self.tested_alphas_logfile}")
            return None

        if len(all_tested) > sample_size:
            sample_records = random.sample(all_tested, sample_size)
        else:
            sample_records = all_tested

        # v7.7: 加锁读取 hopeful_alphas_cache
        with self.hopeful_file_lock:
            hopeful_expressions = {alpha.get('expression') for alpha in self.hopeful_alphas_cache}

        potential_pearls = []
        for record in sample_records:
             # v7.8.1: 确保 record 是字典
            if not isinstance(record, dict): continue
            if record.get('status') != 'COMPLETE' or record.get('expression') in hopeful_expressions:
                continue

            passed_count = record.get('passed_checks', 0)
            fitness = record.get('fitness', -999)

            if passed_count == 3 and fitness > -1.0:
                # v7.9: _calculate_potential_score 已更新
                record['potential_score'] = self._calculate_potential_score(record)
                potential_pearls.append(record)

        if not potential_pearls:
            return None

        potential_pearls.sort(key=lambda x: x.get('potential_score', -999), reverse=True)
        best_pearl = potential_pearls[0]
        logger.info(f"考古学家在 {len(sample_records)} 条记录中发现一颗遗珠！潜力分: {best_pearl['potential_score']:.3f}, Expression: {best_pearl['expression']}")
        return {"expression": best_pearl['expression'], "performance": best_pearl.get('performance', {})}

    # --- v7.9 优化: 综合评分加入 Self-Correlation 惩罚 ---
    def _calculate_combined_score(self, report):
        """计算用于精英池排序和种子选择的综合得分 (v7.9: 加入Self-Corr惩罚)"""
        if not isinstance(report, dict): return -float('inf')
        perf = report.get('performance', {})
        if not isinstance(perf, dict): return -float('inf')

        fitness = perf.get('fitness', -999)
        sharpe = perf.get('sharpe', 0.0)
        turnover = perf.get('turnover', 1.0)
        checks_summary = report.get('checks_summary', '0 PASS')
        passed_count = 0
        try:
            match = re.search(r'(\d+)\s+PASS', checks_summary or '')
            if match: passed_count = int(match.group(1))
        except (ValueError, TypeError): pass

        # --- v7.9 新增: 解析 Self-Correlation ---
        self_corr_value = 0.0 # 默认为 0 (安全)
        try:
            checks_list = perf.get('checks', []) # 从 performance 中获取
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                        self_corr_value = float(check.get('value', 0.0))
                        break
        except (ValueError, TypeError):
            pass # 解析失败, self_corr_value 保持 0.0
        # --- v7.9 结束 ---

        try: fitness_f = float(fitness)
        except (ValueError, TypeError): fitness_f = -999
        try: sharpe_f = float(sharpe)
        except (ValueError, TypeError): sharpe_f = 0.0
        try: turnover_f = float(turnover)
        except (ValueError, TypeError): turnover_f = 1.0

        # --- v7.9 新增: 计算惩罚项 ---
        # WQ 阈值是 0.7。我们只惩罚超过 0.7 的部分。
        self_corr_penalty = 0.0
        if self_corr_value > 0.7:
            # 每超过 0.1，惩罚 0.5 分 (乘以 5.0)
            self_corr_penalty = (self_corr_value - 0.7) * 5.0
        # --- v7.9 结束 ---

        score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
        
        # 调试日志
        # if self_corr_penalty > 0:
        #    logger.info(f"[Score v7.9] Alpha {report.get('expression', 'N/A')[:30]}... Self-Corr: {self_corr_value:.3f}, Penalty: -{self_corr_penalty:.3f}, Final Score: {score:.3f}")

        return score
    # --- v7.9 结束 ---

    # --- v7.9 优化: 同步 Self-Correlation 惩罚 ---
    def _calculate_potential_score(self, record):
        """(v7.9) 为考古记录计算综合得分"""
        if not isinstance(record, dict): return -999
        perf = record.get('performance', {})
        if not isinstance(perf, dict): return -999

        try:
            fitness_f = float(record.get('fitness', -999)) # 来自 record 顶层
            passed_count = int(record.get('passed_checks', 0)) # 来自 record 顶层
            sharpe_f = float(perf.get('sharpe', 0.0)) # 来自 perf
            turnover_f = float(perf.get('turnover', 1.0)) # 来自 perf

            # --- v7.9 新增: 解析 Self-Correlation ---
            self_corr_value = 0.0
            checks_list = perf.get('checks', []) # 从 perf 中获取
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                        self_corr_value = float(check.get('value', 0.0))
                        break
            # --- v7.9 结束 ---

            # --- v7.9 新增: 计算惩罚项 ---
            self_corr_penalty = 0.0
            if self_corr_value > 0.7:
                self_corr_penalty = (self_corr_value - 0.7) * 5.0
            # --- v7.9 结束 ---

            score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
            return score
        except (ValueError, TypeError):
            return -999
    # --- v7.9 结束 ---

    # --- v7.8 优化: 引入“外卡”种子选择 ---
    # --- v7.8.2 优化: 调整分割比例 ---
    def load_evolution_seeds(self, total_sample_size=20, wild_card_count=5):
        seeds = []
        # v7.7: 加锁读写 hopeful_alphas.json 和 self.hopeful_alphas_cache
        with self.hopeful_file_lock:
            if os.path.exists(self.hopeful_alphas_file):
                try:
                    # v7.8.1: 修复空文件处理
                    if os.path.getsize(self.hopeful_alphas_file) < 2:
                        logger.warning(f"加载精英池：{self.hopeful_alphas_file} 文件为空。")
                        self.hopeful_alphas_cache = []
                    else:
                        with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content:
                                loaded_data = json.loads(content)
                                # v7.8.1: 确保加载的是列表
                                if isinstance(loaded_data, list):
                                     self.hopeful_alphas_cache = loaded_data
                                     seeds.extend(self.hopeful_alphas_cache)
                                else:
                                     logger.error(f"加载精英池错误：{self.hopeful_alphas_file} 包含的不是列表。")
                                     self.hopeful_alphas_cache = []
                except (IOError, json.JSONDecodeError) as e:
                    logger.error(f"加载精英池 {self.hopeful_alphas_file} 失败: {e}")
                    self.hopeful_alphas_cache = [] # 出错时重置缓存

            logger.info(f"已加载 {len(self.hopeful_alphas_cache)} 个精英策略。")

        # excavate_one_pearl 已经内部加锁
        pearl = self.excavate_one_pearl()
        if pearl:
            seeds.append(pearl)

        if not seeds:
            logger.warning("精英池为空，且未挖掘到遗珠，无法获取进化种子。")
            return []

        # --- v7.9: 使用更新后的 _calculate_combined_score 排序 ---
        seeds.sort(key=self._calculate_combined_score, reverse=True)

        # 2. 划分精英池和外卡池
        # v7.8.2: 改为 70/30 分割
        cutoff_index = len(seeds) * 7 // 10
        # 确保至少有一个在外卡池 (除非总数 <= 1)
        if cutoff_index == len(seeds) and len(seeds) > 1:
             cutoff_index = len(seeds) - 1

        top_pool = seeds[:cutoff_index]
        bottom_pool = seeds[cutoff_index:] # 后 30% + 遗珠
        logger.info(f"种子池分割: Top {len(top_pool)} (精英), Bottom {len(bottom_pool)} (外卡池)") # v7.8.2: 增加日志

        evolution_seeds = []
        # v7.8.1: 修正精英数量计算
        elite_count = max(0, total_sample_size - wild_card_count)

        # 3. 抽取精英种子
        k_elite = 0
        if top_pool:
            k_elite = min(elite_count, len(top_pool))
            evolution_seeds.extend(random.sample(top_pool, k_elite))

        # 4. 抽取外卡种子
        k_wild = 0
        # v7.8.1: 确保 wild_card_count 不超过 bottom_pool 大小
        actual_wild_card_count = min(wild_card_count, total_sample_size - k_elite) # 确保总数不超过 total_sample_size
        if bottom_pool:
            k_wild = min(actual_wild_card_count, len(bottom_pool))
            evolution_seeds.extend(random.sample(bottom_pool, k_wild))

        # 5. (边缘情况) 如果种子不足，从剩余池中补足
        remaining_needed = total_sample_size - len(evolution_seeds)
        if remaining_needed > 0 and len(seeds) > len(evolution_seeds):
            logger.info(f"种子池较小或抽样后不足，正在补足 {remaining_needed} 个种子...")
            # v7.8.1: 确保 expression 存在
            chosen_expressions = {s['expression'] for s in evolution_seeds if 'expression' in s}
            remaining_pool = [s for s in seeds if 'expression' in s and s['expression'] not in chosen_expressions]

            k_remaining = min(remaining_needed, len(remaining_pool))
            if k_remaining > 0:
                evolution_seeds.extend(random.sample(remaining_pool, k_remaining))
        # --- v7.8.2 结束 ---

        # v7.7: 加锁
        with self.hopeful_file_lock:
            logger.info(f"策略导师将从 {len(self.hopeful_alphas_cache)} 个精英策略中学习模式。")

        logger.info(f"已抽取 {len(evolution_seeds)} 个种子 (目标: {elite_count} 精英, {wild_card_count} 外卡 => 实际: {k_elite} 精英, {k_wild} 外卡) 作为本轮进化父本。")
        return evolution_seeds

    # --- v7.8.1 优化: 修复加权随机指导的“重复” BUG ---
    def analyze_successful_patterns(self, top_k_pool=20, sample_size=7):
        with self.hopeful_file_lock:
            # v7.8.1: 健壮性检查
            if not self.hopeful_alphas_cache or not isinstance(self.hopeful_alphas_cache, list):
                return []
            all_expressions = [alpha.get('expression', '') for alpha in self.hopeful_alphas_cache if isinstance(alpha, dict)]

        operator_pattern = re.compile(r'([a-zA-Z_0-9]+)\s*\(')
        all_operators = []
        for expr in all_expressions:
            if expr and isinstance(expr, str): # v7.8.1: 健壮性检查
                operators_in_expr = operator_pattern.findall(expr)
                all_operators.extend(operators_in_expr)

        if not all_operators:
            return []

        # 1. 获取 Top K 池及其权重
        most_common_pool = Counter(all_operators).most_common(top_k_pool)
        if not most_common_pool:
            return []

        operators = [op for op, count in most_common_pool]
        weights = [count for op, count in most_common_pool]

        # 2. v7.8.1: 循环加权采样，确保唯一性
        selected_guidance = []
        # 确保 k 不大于池子大小
        k = min(sample_size, len(operators))

        # 建立一个临时的 operators/weights 副本，用于采样
        temp_ops = list(operators)
        temp_weights = list(weights)

        while len(selected_guidance) < k and temp_ops:
            # 加权抽取 1 个
            # v7.8.1: 修复 weights 为空时的错误
            if not temp_weights or sum(temp_weights) <= 0: break # 如果权重都为0或列表为空，则停止

            chosen_op = random.choices(temp_ops, weights=temp_weights, k=1)[0]
            selected_guidance.append(chosen_op)

            # 从临时池中移除，确保下次不被抽到
            try:
                idx = temp_ops.index(chosen_op)
                temp_ops.pop(idx)
                temp_weights.pop(idx)
            except ValueError: # 如果元素意外地不在列表中，记录错误并继续
                 logger.error(f"逻辑错误：在 analyze_successful_patterns 中找不到 chosen_op '{chosen_op}'")
                 break # 退出循环避免死循环

        logger.info(f"策略导师分析完成: 从 Top {len(operators)} 模式池中，加权随机抽取 {len(selected_guidance)} 个 *唯一* 指导: {selected_guidance}")
        return selected_guidance
    # --- v7.8.1 结束 ---

    def generate_alpha_idea(self, fields, operators, guidance=None):
        field_list = ", ".join(fields)

        # --- v7.5 优化: 动态混合操作符 ---
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']

        # 从完整列表中排除核心操作符，然后随机抽取
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
            # --- v7.5 优化: 放宽复杂度 ---
            "4.  **Complexity:** Try to keep operators below 15, but more complex and creative combinations are encouraged.",
            "5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression.",
            "6.  **Be Creative:** Do not just combine `close` and `vwap`. Use other fields like `cap`, `adv20`, or `returns`.",
            "**CRITICAL RULE:** Avoid high Self-Correlation (> 0.7). Your expression should be novel and change signal frequently." # v7.9
            # --- v7.5 结束 ---
        ]

        if guidance:
            # v7.8: guidance 现在是随机的
            prompt_lines.append(f"**Strategic Guidance:** Our analysis suggests these patterns are successful: `{', '.join(guidance)}`. Try to incorporate some of these patterns.")

        prompt_lines.extend([
            f"**Available Data Fields:** {field_list}",
            f"**Allowed Operators:** {operator_list}", # v7.5
            "New Alpha Expression:"
        ])
        prompt = "\n".join(prompt_lines)

        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=150, temperature=0.95) # v7.8.1: 增加 token 和温度
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
             # v7.8.1: 更严格的结尾检查和清理
            idea = idea.split(';')[0] # 取第一个分号前的部分
            if idea: idea += ';' # 确保以分号结尾
            else: return None # 如果为空则返回 None

            return {"expression": idea, "settings": {}}
        except Exception as e:
            # --- v7.3 修改: 捕获 LLM API 错误 (500 或 429) ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code == 500:
                logger.warning(f"生成 Alpha 时检测到 LLM Gateway 的 HTTP 500 错误: {e}。将其视为 Rate Limit 信号。")
                self._enter_cooldown(reason="LLM Gateway 500 Error") # <--- v8.0 修改
            elif status_code == 429:
                logger.warning(f"生成 Alpha 时检测到 LLM API 429 Rate Limit: {e}。")
                self._enter_cooldown(reason="LLM 429 Rate Limit") # <--- v8.0 修改
            else:
                logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None
            # --- v7.3 结束 ---

    # --- v7.8 优化: 大胆进化的 Prompt ---
    # --- v7.9 优化: 加入 Self-Corr 规则 ---
    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        base_expression = base_alpha_obj.get('expression')
        base_settings = base_alpha_obj.get('performance', {}).get('settings', self.wq.default_settings)
        
        # v7.9: 传递 Self-Corr 进 Prompt
        base_score_info = self._calculate_combined_score(base_alpha_obj) # 用新函数计算
        
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
        # --- v7.9 结束 ---
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
                # v7.8.1: 更健壮的 JSON 解析
                try:
                    evolved_strategy = json.loads(json_str)
                except json.JSONDecodeError:
                     logger.error(f"进化返回的JSON代码块内容无效: {json_str}")
                     return None

            if not isinstance(evolved_strategy, dict) or 'expression' not in evolved_strategy or 'settings' not in evolved_strategy:
                logger.error("进化返回的JSON格式无效，缺少expression或settings键，或不是字典。")
                return None

             # v7.8.1: 清理 expression
            if 'expression' in evolved_strategy and isinstance(evolved_strategy['expression'], str):
                 expr = evolved_strategy['expression'].strip().replace('`', '')
                 expr = expr.split(';')[0]
                 if expr: evolved_strategy['expression'] = expr + ';'
                 else: evolved_strategy['expression'] = None # 如果清理后为空，则设为 None

            if not evolved_strategy.get('expression'):
                 logger.error("进化返回的 expression 清理后为空。")
                 return None

            return evolved_strategy
        except Exception as e:
            # --- v7.3 修改: 捕获 LLM API 错误 (500 或 429) ---
            status_code = -1
            if hasattr(e, 'status_code'): status_code = e.status_code
            elif hasattr(e, 'response') and e.response: status_code = e.response.status_code

            if status_code == 500:
                logger.warning(f"进化 Alpha 时检测到 LLM Gateway 的 HTTP 500 错误: {e}。将其视为 Rate Limit 信号。")
                self._enter_cooldown(reason="LLM Gateway 500 Error") # <--- v8.0 修改
            elif status_code == 429:
                logger.warning(f"进化 Alpha 时检测到 LLM API 429 Rate Limit: {e}。")
                self._enter_cooldown(reason="LLM 429 Rate Limit") # <--- v8.0 修改
            else:
                logger.error(f"从 API '进化' Alpha 策略失败: {e}")
            return None
            # --- v7.3 结束 ---

    def log_tested_alphas(self, reports_to_log):
        # v7.7: 增加线程锁
        with self.tested_alphas_lock:
            all_reports = []
            if os.path.exists(self.tested_alphas_logfile):
                try:
                    # v7.8.1: 修复空文件处理
                    if os.path.getsize(self.tested_alphas_logfile) > 1:
                        with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content: all_reports = json.loads(content)
                            # v7.8.1: 确保加载的是列表
                            if not isinstance(all_reports, list):
                                 logger.warning(f"{self.tested_alphas_logfile} 内容不是列表，将重置。")
                                 all_reports = []
                except (IOError, json.JSONDecodeError):
                    logger.warning(f"无法解析 {self.tested_alphas_logfile}，将创建新的日志文件。")
                    all_reports = [] # 确保是列表

            all_reports.extend(reports_to_log)
            for report in reports_to_log:
                 # v7.8.1: 健壮性检查
                if isinstance(report, dict) and 'expression' in report:
                    self.tested_alphas.add(report['expression'])

            try:
                with open(self.tested_alphas_logfile, 'w', encoding='utf-8') as f: json.dump(all_reports, f, indent=4, ensure_ascii=False)
            except IOError as e: logger.error(f"写入全量日志文件时出错: {e}")

    def archive_purged_alphas(self, purged_reports, reason="淘汰"):
        # v7.7: 增加线程锁。此方法被 save_hopeful_reports 调用，而 save_hopeful_reports 已经加锁，
        # 所以这里不需要额外加锁。
        if not purged_reports:
            return

        all_archived = []
        if os.path.exists(self.purged_alphas_archive_file):
            try:
                 # v7.8.1: 修复空文件处理
                if os.path.getsize(self.purged_alphas_archive_file) > 1:
                    with open(self.purged_alphas_archive_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content: all_archived = json.loads(content)
                         # v7.8.1: 确保加载的是列表
                        if not isinstance(all_archived, list):
                             logger.warning(f"{self.purged_alphas_archive_file} 内容不是列表，将重置。")
                             all_archived = []
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析归档文件 {self.purged_alphas_archive_file}，将创建新文件。")
                all_archived = [] # 确保是列表

        for report in purged_reports:
             # v7.8.1: 健壮性检查
            if isinstance(report, dict):
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
        # v7.7: 增加线程锁
        with self.hopeful_file_lock:
            existing_reports = []
            if os.path.exists(self.hopeful_alphas_file):
                try:
                    # v7.8.1: 修复空文件处理
                    if os.path.getsize(self.hopeful_alphas_file) > 1:
                        with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content: existing_reports = json.loads(content)
                             # v7.8.1: 确保加载的是列表
                            if not isinstance(existing_reports, list):
                                 logger.warning(f"{self.hopeful_alphas_file} 内容不是列表，将重置。")
                                 existing_reports = []
                except (IOError, json.JSONDecodeError):
                    logger.warning(f"无法解析 {self.hopeful_alphas_file}，将创建新的精华文件。")
                    existing_reports = [] # 确保是列表

            combined_reports = existing_reports + new_hopeful_reports

            purged_reports = []
            archived_reports = []

            # v7.8.1: 健壮性检查
            unique_reports_map = {}
            for report in combined_reports:
                if isinstance(report, dict) and 'expression' in report:
                     unique_reports_map[report.get('expression')] = report

            for report in unique_reports_map.values():
                fitness = report.get('performance', {}).get('fitness', -999)
                checks_summary = report.get('checks_summary', '0 PASS')
                passed_count = 0
                try:
                    match = re.search(r'(\d+)\s+PASS', checks_summary or '')
                    if match: passed_count = int(match.group(1))
                except (ValueError, TypeError): pass

                try: fitness_float = float(fitness)
                except (ValueError, TypeError): fitness_float = -999

                # --- v7.9: 增加 Self-Correlation 检查 ---
                self_corr_value = 0.0 # 默认为 0 (安全)
                try:
                    checks_list = report.get('performance', {}).get('checks', [])
                    if isinstance(checks_list, list):
                        for check in checks_list:
                            if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                                self_corr_value = float(check.get('value', 0.0))
                                break
                except (ValueError, TypeError):
                    pass # 如果解析失败，保持 0.0
                
                is_self_corr_ok = self_corr_value < 0.7 
                # --- v7.9 结束 ---

                is_high_quality = fitness_float > 0 and passed_count >= 4
                is_high_potential = fitness_float > -0.5 and passed_count >= 5

                # v7.9: 更新判断逻辑
                if (is_high_quality or is_high_potential) and is_self_corr_ok:
                    purged_reports.append(report)
                elif (is_high_quality or is_high_potential) and not is_self_corr_ok:
                    # 达到了 Fitness/Checks，但 Self-Corr 太高，拒绝并归档
                    logger.warning(f"策略 {report.get('expression', '')[:40]}... 因 Self-Correlation 过高 ({self_corr_value:.3f} > 0.7) 被精英池拒绝（即使 Fitness/Checks 达标）。")
                    archived_reports.append(report) # 归档这些“坏基因”
                elif any(isinstance(r, dict) and r.get('expression') == report.get('expression') for r in existing_reports):
                    # 未达标，且是旧策略，归档
                    archived_reports.append(report)
                # else:
                #   未达标，且是新策略，暂时不归档，也不保留 (默认丢弃)

            logger.info(f"精英池清洗: {len(unique_reports_map)} -> {len(purged_reports)} (识别出 {len(archived_reports)} 个过时/高相关性策略)")

            self.archive_purged_alphas(archived_reports, reason="标准清洗 (含Self-Corr > 0.7)")

            # v7.9: 使用更新后的 _calculate_combined_score 排序
            purged_reports.sort(key=self._calculate_combined_score, reverse=True)

            final_pool = purged_reports[:max_pool_size]

            if len(purged_reports) > max_pool_size:
                eliminated = purged_reports[max_pool_size:]
                logger.info(f"精英池末位淘汰: {len(purged_reports)} -> {len(final_pool)} (保留综合评分排名前 {max_pool_size} 的策略)")
                self.archive_purged_alphas(eliminated, reason="末位淘汰")

            try:
                with open(self.hopeful_alphas_file, 'w', encoding='utf-8') as f:
                    json.dump(final_pool, f, indent=4, ensure_ascii=False)
                logger.info(f"已将 {len(new_hopeful_reports)} 份新战报处理完毕，并完成了精英池的动态维护。当前池中共有 {len(final_pool)} 个策略。")

                # v7.7: 更新内存中的缓存
                self.hopeful_alphas_cache = final_pool

            except IOError as e:
                logger.error(f"保存精华战报文件时出错: {e}")

    # --- v7.7 新增: 消费者 (Worker) 线程 ---
    # --- v7.8.3 修复: 修正 RATE_LIMIT 逻辑中的 task_done() 重复调用 BUG ---
    def _consumer_worker(self):
        """消费者工作线程，从队列中获取策略并执行测试。"""
        while True:
            strategy = None
            try:
                # 1. 从队列获取任务
                strategy = self.strategy_queue.get()
                if strategy is None: # 退出信号
                    self.strategy_queue.task_done()
                    break

                # v7.8.1: 健壮性检查
                if not isinstance(strategy, dict) or 'expression' not in strategy:
                     logger.warning("从队列中获取到无效的 strategy 对象，已丢弃。")
                     self.strategy_queue.task_done()
                     continue

                idea_expr = strategy['expression']
                # v7.8.1: 修复 idea_expr 为 None 的情况
                if not idea_expr:
                    logger.warning("从队列中获取到 expression 为空的 strategy 对象，已丢弃。")
                    self.strategy_queue.task_done()
                    continue

                logger.info(f"取得策略: {idea_expr[:60]}... (队列剩余: {self.strategy_queue.qsize()})")
                log_report = {"expression": idea_expr, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

                # 2. 执行 WQ 测试
                result = self.wq.test_alpha(idea_expr, strategy['settings'])

                # 3. 处理 WQ 429 Rate Limit (核心)
                if result == "RATE_LIMIT":
                    logger.warning(f"遭遇 WQ 429 (针对: {idea_expr})。")
                    
                    # --- v8.0: 动态触发冷却 ---
                    config = load_system_config()
                    current_wq_cooldown = config.get("wq_api_cooldown", 30)
                    logger.info(f"触发 {current_wq_cooldown}s 冷却... 策略将放回队列重试。")
                    self._enter_cooldown(reason="WorldQuant 429 Rate Limit")
                    time.sleep(current_wq_cooldown) # 睡眠时长也动态读取
                    # --- v8.0 结束 ---

                    try:
                        self.strategy_queue.put(strategy)
                        logger.info(f"策略 {idea_expr[:60]}... 已放回队列。")
                    except queue.Full:
                         logger.error(f"尝试放回策略 {idea_expr[:60]}... 时队列已满！该策略将被丢弃。")
                         # v7.8.3: 策略放回失败 (Full)，这个 get() 任务被丢弃了，
                         # 我们必须在这里调用 task_done() 来平衡 get()。
                         self.strategy_queue.task_done()
                         continue # <--- 确保在丢弃时也 continue

                    # v7.8.3: 无论策略是成功放回 (put) 还是放回失败 (Full)，
                    # 我们都必须 continue 来跳过 finally 块中的 task_done()。
                    continue 

                # 4. 处理 ERROR (黑名单逻辑)
                if isinstance(result, dict) and result.get("status") == "ERROR":
                    log_report["status"] = "ERROR"
                    self.log_tested_alphas([log_report]) # v7.7: 立即记录

                    error_message = ""
                    regular_result = result.get("regular", {})
                    if isinstance(regular_result, dict):
                         regular_errors = regular_result.get("errors", [])
                         if regular_errors and isinstance(regular_errors, list) and len(regular_errors) > 0 and isinstance(regular_errors[0], dict):
                             error_message = regular_errors[0].get("message", "")
                    if not error_message: error_message = result.get("message", "")
                    match = re.search(r"(Unknown function|unknown operator|unknown variable) '(\w+)'", error_message or "")

                    if match:
                        error_type = match.group(1)
                        bad_identifier = match.group(2)
                        should_blacklist = False
                        if error_type == "unknown variable":
                            should_blacklist = True
                            logger.warning(f"检测到无效变量: '{bad_identifier}'。WQ API 报告其未知。")
                        elif error_type in ["Unknown function", "unknown operator"]:
                            if bad_identifier not in (self.fields if isinstance(self.fields, list) else []):
                                should_blacklist = True
                                logger.warning(f"检测到无效函数/操作符: '{bad_identifier}'。")
                            else:
                                logger.info(f"Alpha 模拟出错: '{bad_identifier}' 是一个数据字段，但被误用为函数。已记录，不计入黑名单。")
                        if should_blacklist:
                            self.update_blacklist_count(bad_identifier)
                    else:
                        logger.warning(f"Alpha 模拟出错 (非特定标识符错误)，已记录: {idea_expr} | Error: {str(error_message)[:200]}...")
                    
                    continue 

                # 5. 处理 TIMEOUT
                if result in ["TIMEOUT"]:
                    log_report["status"] = result
                    self.log_tested_alphas([log_report])
                    logger.warning(f"Alpha 模拟{result}，已记录并丢弃 (不计入黑名单): {idea_expr}")
                    
                    continue 

                # 6. 处理 COMPLETE
                if isinstance(result, dict):
                    is_stats = result.get("is", {})
                    alpha_id = result.get("id")
                    if not isinstance(is_stats, dict) or not alpha_id:
                        logger.warning(f"模拟返回不完整 (缺少 'is' 或 'id')，已丢弃: {idea_expr}")
                        continue 

                    checks = is_stats.get("checks", [])
                    passed_count = 0
                    failed_count = 0
                    pending_count = 0
                    if isinstance(checks, list):
                         passed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PASS")
                         failed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "FAIL")
                         pending_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PENDING")

                    fitness = is_stats.get('fitness', -999)
                    try: fitness_float = float(fitness)
                    except (ValueError, TypeError): fitness_float = -999

                    log_report["status"] = "COMPLETE"
                    log_report["fitness"] = fitness_float
                    log_report["passed_checks"] = passed_count
                    log_report["performance"] = is_stats # v7.9: 确保完整的 is_stats 被存入
                    self.log_tested_alphas([log_report])
                    checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                    # --- v7.9: 增加 Self-Correlation 检查 (用于日志) ---
                    self_corr_value = 0.0
                    try:
                        if isinstance(checks, list):
                            for check in checks:
                                if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                                    self_corr_value = float(check.get('value', 0.0))
                                    break
                    except (ValueError, TypeError): pass
                    is_self_corr_ok = self_corr_value < 0.7
                    # --- v7.9 结束 ---

                    is_high_quality = fitness_float > 0 and passed_count >= 4
                    is_high_potential = fitness_float > -0.5 and passed_count >= 5

                    # v7.9: 更新判断逻辑
                    if (is_high_quality or is_high_potential) and is_self_corr_ok:
                        if is_high_potential and not is_high_quality:
                            logger.info(f"发现一个高潜力策略 (Fitness < 0, 但 Checks >= 5)，破格录用！ Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                        else:
                            logger.info(f"发现一个高质量策略！ Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")

                        regular_code = result.get("regular", {}).get("code") if isinstance(result.get("regular"), dict) else None
                        hopeful_report = {
                            "expression": regular_code or idea_expr,
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
                        self.save_hopeful_reports([hopeful_report]) # v7.9: save_hopeful_reports 内部会再次检查 self-corr
                    
                    elif (is_high_quality or is_high_potential) and not is_self_corr_ok:
                         logger.info(f"策略因 Self-Correlation 过高被拒绝。Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                         # v7.9: 我们在 save_hopeful_reports 中归档，这里只打印日志
                    
                    else:
                        logger.info(f"策略未达到高质量标准，已丢弃。Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                else:
                     logger.error(f"收到未知的模拟结果类型: {type(result)} for alpha: {idea_expr}")

            except Exception as exc:
                expr_for_log = "UNKNOWN"
                if isinstance(strategy, dict) and 'expression' in strategy:
                     expr_for_log = strategy['expression']
                logger.error(f"处理策略 '{expr_for_log}' 的结果时发生意外错误: {exc}", exc_info=True)
                if isinstance(strategy, dict) and 'expression' in strategy:
                    try:
                        log_report = {"expression": strategy['expression'], "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        log_report["status"] = "EXCEPTION_WORKER"
                        self.log_tested_alphas([log_report])
                    except Exception as log_exc:
                        logger.critical(f"在异常处理中再次发生错误，无法记录: {log_exc}")
            finally:
                # 7. 标记任务完成 (确保即使出错也调用)
                self.strategy_queue.task_done()
    # --- v7.8.3 修复结束 ---


    # --- v7.7 重构: run 方法现在是 生产者 ---
    def run(self, mode='discover', sleep_time=10):

        logger.info(f"Alpha 生成器启动 | 版本: {CURRENT_GENERATOR_VERSION} | 模式: {mode.upper()} | 并发 Workers: {self.concurrency_level} | 队列大小: {self.queue_max_size}") # v8.0

        self.fields = self.wq.get_data_fields()
        self.operators = self.wq.get_operators()

        if self.operators == "RATE_LIMIT":
            logger.critical("获取操作符时遭遇 WorldQuant 429，触发冷却。")
            self._enter_cooldown(reason="WorldQuant 429 Rate Limit") # v8.0: 动态冷却

        if not self.fields or not self.operators or self.operators == "RATE_LIMIT":
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            if self.operators != "RATE_LIMIT":
                time.sleep(60)

        # --- v7.7: 启动消费者 (Workers) ---
        logger.info(f"正在启动 {self.concurrency_level} 个消费者 (worker) 线程...")
        for i in range(self.concurrency_level):
            t = threading.Thread(target=self._consumer_worker, name=f"Worker-{i+1}", daemon=True)
            t.start()
            self.consumer_threads.append(t)
        # --- v7.7 结束 ---

        evolution_seeds = []
        strategic_guidance = []

        # --- v7.7: 生产者 (Producer) 循环 ---
        while True:
            try:
                # 1. 检查 LLM 冷却状态 (v8.0: _rate_limit_until 是动态设置的)
                if time.time() < self._rate_limit_until:
                    remaining = self._rate_limit_until - time.time()
                    logger.info(f"[生产者] 当前处于冷却期。将在 {remaining/60:.1f} 分钟后恢复...")
                    time.sleep(min(remaining, 300))
                    continue

                # 2. 检查 WQ 字段/操作符
                # v7.8.1: 每次都获取，防止过时 (虽然 WQ API 可能缓存)
                self.fields = self.wq.get_data_fields()
                self.operators = self.wq.get_operators()
                if self.operators == "RATE_LIMIT":
                     logger.critical("[生产者] 获取操作符时遭遇 WorldQuant 429，触发冷却。")
                     self._enter_cooldown(reason="WorldQuant 429 Rate Limit") # v8.0: 动态冷却
                     continue
                if not self.fields or not self.operators:
                     logger.error("[生产者] 无法获取字段或操作符，将在60秒后重试。")
                     time.sleep(60)
                     continue


                # 3. (Evolve 模式) 更新种子和指导
                # v7.8: 每次循环都重新加载，以获取最新数据 (已加锁)
                if mode == 'evolve':
                    # v7.9: load_evolution_seeds 内部使用新评分
                    evolution_seeds = self.load_evolution_seeds()
                    if not evolution_seeds:
                        mode = 'discover'
                        logger.warning("[生产者] 进化模式无法启动（无可用种子），已自动切换到发现模式。")
                    else:
                        # v7.8.1: analyze_successful_patterns 已更新 (无重复)
                        strategic_guidance = self.analyze_successful_patterns()

                # 4. 检查队列是否已满
                if self.strategy_queue.qsize() >= self.queue_max_size:
                    logger.info(f"[生产者] 队列已满 ({self.strategy_queue.qsize()}/{self.queue_max_size})，暂停生成 10 秒...")
                    time.sleep(10)
                    continue

                logger.info(f"[生产者] [{mode.upper()}] 开始生成 1 个新 Alpha... (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")

                # 5. 生成新策略
                idea = None
                if mode == 'discover':
                    idea = self.generate_alpha_idea(self.fields, self.operators, guidance=strategic_guidance)
                elif mode == 'evolve':
                    # v7.8.1: 防止 evolution_seeds 为空时出错
                    if not evolution_seeds:
                         logger.warning("[生产者] 进化模式种子列表为空，跳过本轮生成。")
                         time.sleep(sleep_time) # 仍然休眠
                         continue
                    base_alpha_obj = random.choice(evolution_seeds)
                    # v7.9: generate_evolved_alpha_idea 已更新 (Prompt 加入 Self-Corr 规则)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance)

                # 6. 预检
                # v7.8.1: 健壮性检查
                if isinstance(idea, dict) and idea.get("expression"):
                    expr = idea.get("expression")

                    # 检查是否已测试 (v7.7: 线程安全)
                    with self.tested_alphas_lock:
                        is_tested = expr in self.tested_alphas
                    if is_tested:
                        logger.info(f"[生产者] 策略 {expr[:60]}... 已被测试过，丢弃。")
                        continue

                    # 检查语法 (v7.6)
                    if is_alpha_syntactically_suspicious(expr):
                        continue # 日志已在函数内打印

                    # 检查黑名单 (v7.6.2)
                    if self.is_using_blacklisted_identifier(expr):
                        continue # 日志已在函数内打印

                    # 7. 放入队列
                    # v7.8.1: 使用 try-except 增加健壮性
                    try:
                         self.strategy_queue.put(idea)
                         logger.info(f"[生产者] 新策略已生成并通过预检，放入队列。 (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")
                    except queue.Full:
                         logger.error("[生产者] 尝试放入策略时队列已满！") # 理论上不应发生，因为前面有检查

                else:
                    logger.warning("[生产者] LLM未能生成有效的 Alpha 策略。")

                logger.info(f"[生产者] 本轮生成结束。等待{sleep_time}秒开始下一轮...")
                time.sleep(sleep_time)

            except Exception as e:
                logger.critical(f"[生产者] 循环发生致命错误: {e}", exc_info=True)
                time.sleep(60) # 发生严重错误时，暂停1分钟

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator using a generic API endpoint')
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle (v7.7: 已弃用，但保留)")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    
    # --- v8.0: 移除静态参数，改为从 config 文件读取 ---
    # parser.add_argument('--concurrency', type=int, default=2, help="Number of concurrent simulation workers (v7.7)")
    # parser.add_argument('--sleep', type=int, default=10, help="Seconds to wait between generation cycles (Producer sleep time)")
    # --- v8.0 结束 ---
    
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve'], help="Generation mode")
    parser.add_argument('--log-file', type=str, default='alpha_generator.log', help="Name of the log file in the logs directory")
    args = parser.parse_args()

    setup_logging(args.log_file)
    
    # --- v8.0: 读取静态配置 ---
    logger.info(f"正在加载 {SYSTEM_CONFIG_FILE} 以确定启动参数...")
    config = load_system_config()
    
    if args.mode == 'discover':
        concurrency = config.get("miner_concurrency", 1)
        sleep_time = config.get("miner_sleep", 120)
        logger.info(f"[v8.0 Config] 启动 Miner (discover) 模式: Concurrency={concurrency}, Sleep={sleep_time}s")
    elif args.mode == 'evolve':
        concurrency = config.get("evolver_concurrency", 1)
        sleep_time = config.get("evolver_sleep", 120)
        logger.info(f"[v8.0 Config] 启动 Evolver (evolve) 模式: Concurrency={concurrency}, Sleep={sleep_time}s")
    else:
        logger.error(f"未知的模式: {args.mode}。使用默认值 1/120。")
        concurrency = 1
        sleep_time = 120
    # --- v8.0 结束 ---

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
                
                # --- v8.0: 动态读取 WQ Cooldown ---
                config_init = load_system_config()
                wq_cooldown_init = config_init.get("wq_api_cooldown", 30)
                logger.warning(f"将进入 {wq_cooldown_init} 秒冷却期...")
                time.sleep(wq_cooldown_init)
                # --- v8.0 结束 ---

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
        # v8.0: 使用从配置中读取的静态参数
        generator = AlphaGenerator(wq_client,
                                 api_config_path=args.api_config_path,
                                 batch_size=args.batch_size,
                                 concurrency_level=concurrency) # <--- v8.0 修改

        # v8.0: 使用从配置中读取的静态参数
        generator.run(mode=args.mode, sleep_time=sleep_time) # <--- v8.0 修改

    except Exception as e:
        logger.critical(f"生成器运行时发生致命错误: {e}", exc_info=True)