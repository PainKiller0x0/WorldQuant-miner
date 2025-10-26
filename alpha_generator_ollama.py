# --- alpha_generator_ollama.py v9.0.0 (Modularized) ---
import argparse
import logging
import json
import os
import time
import requests # 保留，用于 __main__ 中的 WQ 客户端初始化异常捕获
import random
from datetime import datetime
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import queue

# --- v9.0 模块化导入 ---
from utils import setup_logging, load_system_config
from wq_client import WorldQuant
from llm_provider import LLMProvider
# --- v9.0 结束 ---

CURRENT_GENERATOR_VERSION = "v9.0.0 (Modularized)" # v9.0

# --- v9.0 移除: load_system_config (已移至 utils) ---

# --- BUG 修复: 将 logger 定义移至全局作用域 ---
logger = logging.getLogger(__name__)
# --- 修复结束 ---

# --- v8.0: 移除硬编码的 Cooldowns (保留) ---

# --- v7.6 调整: 黑名单文件及计数 ---
INVALID_FUNCTIONS_FILE = "invalid_functions.json"
BLACKLIST_MAX_STRIKES = 3 # "事不过三"
# --- v7.6 结束 ---

# --- v9.0 移除: setup_logging (已移至 utils) ---

# --- v9.0 移除: WorldQuant Class (已移至 wq_client) ---


# --- 语法预检函数 (保留在主模块) ---
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

class AlphaGenerator:
    # v7.7: __init__ 签名改变
    def __init__(self, wq: WorldQuant, api_config_path, batch_size=5, concurrency_level=2): # v9.0: 显式类型提示
        self.wq = wq # v9.0: 传入 WQ 客户端实例
        self.batch_size = batch_size
        
        # --- v9.0: 初始化 LLMProvider ---
        try:
            self.llm = LLMProvider(api_config_path=api_config_path)
        except Exception as e:
            logger.critical(f"初始化 LLMProvider 失败: {e}")
            raise
        # --- v9.0 结束 ---

        self.hopeful_alphas_file = "hopeful_alphas.json"
        self.tested_alphas_logfile = "tested_alphas_log.json"
        self.purged_alphas_archive_file = "purged_alphas_archive.json"

        # --- v7.7 新增: 线程安全锁 ---
        self.tested_alphas_lock = threading.Lock()
        self.hopeful_file_lock = threading.Lock()
        self.blacklist_lock = threading.Lock()
        # --- v7.7 结束 ---

        self.tested_alphas = self.load_tested_alphas()
        self.hopeful_alphas_cache = []

        # --- v8.0: 冷却状态 (保留) ---
        self._rate_limit_until = 0
        # --- v8.0 结束 ---

        # --- v7.6.2 调整: "事不过三"标识符黑名单 ---
        self.invalid_functions_file = INVALID_FUNCTIONS_FILE
        self.blacklist_counts = self.load_blacklist_counts()
        self.blacklist_max_strikes = BLACKLIST_MAX_STRIKES
        self.identifier_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z_0-9]*)\b')
        self.fields = [] # 用于存储字段列表
        # --- v7.6.2 结束 ---

        # --- v7.7 新增: 生产者-消费者队列 ---
        self.concurrency_level = concurrency_level
        self.queue_max_size = self.concurrency_level * 2
        self.strategy_queue = queue.Queue(maxsize=self.queue_max_size)
        self.consumer_threads = []
        # --- v7.7 结束 ---

    # --- v8.0: 冷却触发器 (保留) ---
    def _enter_cooldown(self, reason="Rate Limit"):
        """
        触发冷却期 (v8.0: 动态从 system_config.json 读取时长)
        """
        config = load_system_config() # v9.0: 使用导入的函数
        duration_seconds = 3600 # 默认回退
        
        if reason == "WorldQuant 429 Rate Limit":
            duration_seconds = config.get("wq_api_cooldown", 30)
        elif reason in ["LLM Gateway 500 Error", "LLM 429 Rate Limit"]:
            duration_seconds = config.get("llm_api_cooldown", 3600)
        else:
            duration_seconds = config.get("wq_api_cooldown", 60)

        self._rate_limit_until = time.time() + duration_seconds
        duration_minutes = duration_seconds / 60
        logger.warning(f"检测到 {reason}。脚本将进入冷却期 {duration_minutes:.0f} 分钟 ({duration_seconds} 秒)，直到 {datetime.fromtimestamp(self._rate_limit_until).strftime('%Y-%m-%d %H:%M:%S')}")
    # --- v8.0 结束 ---

    # --- v9.0: 所有文件 IO 和业务逻辑函数 (load_tested_alphas, load_blacklist_counts, ... _calculate_combined_score, 等) 保持不变 ---
    # (此处省略所有未更改的函数: load_tested_alphas, load_blacklist_counts, update_blacklist_count, is_using_blacklisted_identifier, 
    #  excavate_one_pearl, _calculate_combined_score, _calculate_potential_score, load_evolution_seeds, analyze_successful_patterns,
    #  log_tested_alphas, archive_purged_alphas, save_hopeful_reports)

    # --- v7.7: 业务逻辑函数 (保留) ---
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

    def load_blacklist_counts(self):
        with self.blacklist_lock:
            if not os.path.exists(self.invalid_functions_file):
                logger.info("无效标识符计数文件(invalid_functions.json)不存在，将创建新的。")
                return {}
            try:
                with open(self.invalid_functions_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content: return {}
                    data = json.loads(content)
                    if not isinstance(data, dict):
                        logger.warning(f"{self.invalid_functions_file} 格式不正确 (不是字典)，将重置。")
                        return {}
                    logger.info(f"成功加载 {len(data)} 个标识符的黑名单计数。")
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 {self.invalid_functions_file} 出错: {e}, 将创建新的。")
                return {}

    def update_blacklist_count(self, identifier_name):
        with self.blacklist_lock:
            current_counts = self.load_blacklist_counts() # v7.7.1 修复
            current_count = current_counts.get(identifier_name, 0)
            current_count += 1
            current_counts[identifier_name] = current_count
            try:
                with open(self.invalid_functions_file, 'w', encoding='utf-8') as f:
                    json.dump(current_counts, f, indent=4)
                self.blacklist_counts = current_counts # 同步内存
                if current_count < self.blacklist_max_strikes:
                    logger.warning(f"检测到无效标识符: '{identifier_name}'。计数: {current_count}/{self.blacklist_max_strikes}。")
                else:
                    logger.critical(f"'{identifier_name}' 已达到 {current_count}/{self.blacklist_max_strikes} 次计数，将被永久拉黑。")
            except IOError as e:
                logger.error(f"保存黑名单计数文件时出错: {e}")

    def is_using_blacklisted_identifier(self, alpha_code: str) -> bool:
        with self.blacklist_lock:
            current_counts = self.blacklist_counts
        if not current_counts: return False
        found_identifiers = self.identifier_pattern.findall(alpha_code)
        if not found_identifiers: return False
        for identifier in found_identifiers:
            if identifier in current_counts and current_counts[identifier] >= self.blacklist_max_strikes:
                logger.warning(f"预检拦截: Alpha '{alpha_code}' 包含了已被拉黑的标识符 '{identifier}' (计数: {current_counts[identifier]}/{self.blacklist_max_strikes})。")
                return True
        return False

    def excavate_one_pearl(self, sample_size=200):
        all_tested = []
        if not os.path.exists(self.tested_alphas_logfile): return None
        try:
            with self.tested_alphas_lock:
                if os.path.getsize(self.tested_alphas_logfile) < 2:
                     logger.info(f"考古挖掘：{self.tested_alphas_logfile} 文件为空。")
                     return None
                with open(self.tested_alphas_logfile, 'r') as f:
                    all_tested = json.load(f)
        except (IOError, json.JSONDecodeError):
            logger.error(f"考古挖掘失败：无法读取 {self.tested_alphas_logfile}")
            return None
        
        sample_records = random.sample(all_tested, min(len(all_tested), sample_size))
        with self.hopeful_file_lock:
            hopeful_expressions = {alpha.get('expression') for alpha in self.hopeful_alphas_cache}

        potential_pearls = []
        for record in sample_records:
            if not isinstance(record, dict): continue
            if record.get('status') != 'COMPLETE' or record.get('expression') in hopeful_expressions:
                continue
            passed_count = record.get('passed_checks', 0)
            fitness = record.get('fitness', -999)
            if passed_count == 3 and fitness > -1.0:
                record['potential_score'] = self._calculate_potential_score(record) # v7.9
                potential_pearls.append(record)

        if not potential_pearls: return None
        potential_pearls.sort(key=lambda x: x.get('potential_score', -999), reverse=True)
        best_pearl = potential_pearls[0]
        logger.info(f"考古学家在 {len(sample_records)} 条记录中发现一颗遗珠！潜力分: {best_pearl['potential_score']:.3f}, Expression: {best_pearl['expression']}")
        return {"expression": best_pearl['expression'], "performance": best_pearl.get('performance', {})}

    def _calculate_combined_score(self, report):
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
        self_corr_value = 0.0 # v7.9
        try:
            checks_list = perf.get('checks', [])
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                        self_corr_value = float(check.get('value', 0.0))
                        break
        except (ValueError, TypeError): pass
        try: fitness_f = float(fitness)
        except (ValueError, TypeError): fitness_f = -999
        try: sharpe_f = float(sharpe)
        except (ValueError, TypeError): sharpe_f = 0.0
        try: turnover_f = float(turnover)
        except (ValueError, TypeError): turnover_f = 1.0
        self_corr_penalty = 0.0 # v7.9
        if self_corr_value > 0.7:
            self_corr_penalty = (self_corr_value - 0.7) * 5.0
        score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
        return score

    def _calculate_potential_score(self, record):
        if not isinstance(record, dict): return -999
        perf = record.get('performance', {})
        if not isinstance(perf, dict): return -999
        try:
            fitness_f = float(record.get('fitness', -999))
            passed_count = int(record.get('passed_checks', 0))
            sharpe_f = float(perf.get('sharpe', 0.0))
            turnover_f = float(perf.get('turnover', 1.0))
            self_corr_value = 0.0 # v7.9
            checks_list = perf.get('checks', [])
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                        self_corr_value = float(check.get('value', 0.0))
                        break
            self_corr_penalty = 0.0 # v7.9
            if self_corr_value > 0.7:
                self_corr_penalty = (self_corr_value - 0.7) * 5.0
            score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
            return score
        except (ValueError, TypeError):
            return -999

    def load_evolution_seeds(self, total_sample_size=20, wild_card_count=5):
        seeds = []
        with self.hopeful_file_lock:
            if os.path.exists(self.hopeful_alphas_file):
                try:
                    if os.path.getsize(self.hopeful_alphas_file) < 2:
                        logger.warning(f"加载精英池：{self.hopeful_alphas_file} 文件为空。")
                        self.hopeful_alphas_cache = []
                    else:
                        with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content:
                                loaded_data = json.loads(content)
                                if isinstance(loaded_data, list):
                                     self.hopeful_alphas_cache = loaded_data
                                     seeds.extend(self.hopeful_alphas_cache)
                                else:
                                     logger.error(f"加载精英池错误：{self.hopeful_alphas_file} 包含的不是列表。")
                                     self.hopeful_alphas_cache = []
                except (IOError, json.JSONDecodeError) as e:
                    logger.error(f"加载精英池 {self.hopeful_alphas_file} 失败: {e}")
                    self.hopeful_alphas_cache = []
            logger.info(f"已加载 {len(self.hopeful_alphas_cache)} 个精英策略。")

        pearl = self.excavate_one_pearl()
        if pearl:
            seeds.append(pearl)
        if not seeds:
            logger.warning("精英池为空，且未挖掘到遗珠，无法获取进化种子。")
            return []

        # v9.0: 为种子计算内部得分
        for seed in seeds:
            seed['internal_score'] = self._calculate_combined_score(seed)
            
        seeds.sort(key=lambda x: x['internal_score'], reverse=True) # v9.0: 使用新key排序
        cutoff_index = len(seeds) * 7 // 10
        if cutoff_index == len(seeds) and len(seeds) > 1:
             cutoff_index = len(seeds) - 1
        top_pool = seeds[:cutoff_index]
        bottom_pool = seeds[cutoff_index:]
        logger.info(f"种子池分割: Top {len(top_pool)} (精英), Bottom {len(bottom_pool)} (外卡池)")

        evolution_seeds = []
        elite_count = max(0, total_sample_size - wild_card_count)
        k_elite = 0
        if top_pool:
            k_elite = min(elite_count, len(top_pool))
            evolution_seeds.extend(random.sample(top_pool, k_elite))
        k_wild = 0
        actual_wild_card_count = min(wild_card_count, total_sample_size - k_elite)
        if bottom_pool:
            k_wild = min(actual_wild_card_count, len(bottom_pool))
            evolution_seeds.extend(random.sample(bottom_pool, k_wild))
        remaining_needed = total_sample_size - len(evolution_seeds)
        if remaining_needed > 0 and len(seeds) > len(evolution_seeds):
            logger.info(f"种子池较小或抽样后不足，正在补足 {remaining_needed} 个种子...")
            chosen_expressions = {s['expression'] for s in evolution_seeds if 'expression' in s}
            remaining_pool = [s for s in seeds if 'expression' in s and s['expression'] not in chosen_expressions]
            k_remaining = min(remaining_needed, len(remaining_pool))
            if k_remaining > 0:
                evolution_seeds.extend(random.sample(remaining_pool, k_remaining))
        
        with self.hopeful_file_lock:
            logger.info(f"策略导师将从 {len(self.hopeful_alphas_cache)} 个精英策略中学习模式。")
        logger.info(f"已抽取 {len(evolution_seeds)} 个种子 (目标: {elite_count} 精英, {wild_card_count} 外卡 => 实际: {k_elite} 精英, {k_wild} 外卡) 作为本轮进化父本。")
        return evolution_seeds

    def analyze_successful_patterns(self, top_k_pool=20, sample_size=7):
        with self.hopeful_file_lock:
            if not self.hopeful_alphas_cache or not isinstance(self.hopeful_alphas_cache, list):
                return []
            all_expressions = [alpha.get('expression', '') for alpha in self.hopeful_alphas_cache if isinstance(alpha, dict)]
        operator_pattern = re.compile(r'([a-zA-Z_0-9]+)\s*\(')
        all_operators = []
        for expr in all_expressions:
            if expr and isinstance(expr, str):
                operators_in_expr = operator_pattern.findall(expr)
                all_operators.extend(operators_in_expr)
        if not all_operators: return []

        most_common_pool = Counter(all_operators).most_common(top_k_pool)
        if not most_common_pool: return []
        operators = [op for op, count in most_common_pool]
        weights = [count for op, count in most_common_pool]
        selected_guidance = []
        k = min(sample_size, len(operators))
        temp_ops = list(operators)
        temp_weights = list(weights)
        while len(selected_guidance) < k and temp_ops:
            if not temp_weights or sum(temp_weights) <= 0: break
            chosen_op = random.choices(temp_ops, weights=temp_weights, k=1)[0]
            selected_guidance.append(chosen_op)
            try:
                idx = temp_ops.index(chosen_op)
                temp_ops.pop(idx)
                temp_weights.pop(idx)
            except ValueError:
                 logger.error(f"逻辑错误：在 analyze_successful_patterns 中找不到 chosen_op '{chosen_op}'")
                 break
        logger.info(f"策略导师分析完成: 从 Top {len(operators)} 模式池中，加权随机抽取 {len(selected_guidance)} 个 *唯一* 指导: {selected_guidance}")
        return selected_guidance

    # --- v9.0 重构: 委托给 LLMProvider ---
    def generate_alpha_idea(self, fields, operators, guidance=None):
        """
        v9.0: 委托 LLMProvider 生成，并处理冷却信号。
        """
        result = self.llm.generate_alpha_idea(fields, operators, guidance)
        
        if result == "RATE_LIMIT":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 RATE_LIMIT (Discover)。")
            # v8.0 逻辑: 使用 "LLM 429 Rate Limit" 理由触发标准 LLM 冷却
            self._enter_cooldown(reason="LLM 429 Rate Limit")
            return None
        
        # result 要么是 idea dict，要么是 None
        return result

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        """
        v9.0: 委托 LLMProvider 进化，并处理冷却信号。
        """
        # v9.0: 传递 base_settings
        base_alpha_obj['performance'] = base_alpha_obj.get('performance', {})
        base_alpha_obj['performance']['settings'] = base_alpha_obj.get('performance', {}).get('settings', self.wq.default_settings)

        result = self.llm.generate_evolved_alpha_idea(base_alpha_obj, guidance=guidance)
        
        if result == "RATE_LIMIT":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 RATE_LIMIT (Evolve)。")
            # v8.0 逻辑: 使用 "LLM 429 Rate Limit" 理由触发标准 LLM 冷却
            self._enter_cooldown(reason="LLM 429 Rate Limit")
            return None
            
        # v9.0: 如果 settings 是空的，从 WQ 客户端填充默认值
        if isinstance(result, dict) and not result.get('settings'):
            result['settings'] = self.wq.default_settings
            logger.info("进化策略未提供 settings，已应用 WQ 默认值。")

        # result 要么是 idea dict，要么是 None
        return result
    # --- v9.0 结束 ---

    def log_tested_alphas(self, reports_to_log):
        with self.tested_alphas_lock:
            all_reports = []
            if os.path.exists(self.tested_alphas_logfile):
                try:
                    if os.path.getsize(self.tested_alphas_logfile) > 1:
                        with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content: all_reports = json.loads(content)
                            if not isinstance(all_reports, list):
                                 logger.warning(f"{self.tested_alphas_logfile} 内容不是列表，将重置。")
                                 all_reports = []
                except (IOError, json.JSONDecodeError):
                    logger.warning(f"无法解析 {self.tested_alphas_logfile}，将创建新的日志文件。")
                    all_reports = []
            all_reports.extend(reports_to_log)
            for report in reports_to_log:
                if isinstance(report, dict) and 'expression' in report:
                    self.tested_alphas.add(report['expression'])
            try:
                with open(self.tested_alphas_logfile, 'w', encoding='utf-8') as f: json.dump(all_reports, f, indent=4, ensure_ascii=False)
            except IOError as e: logger.error(f"写入全量日志文件时出错: {e}")

    def archive_purged_alphas(self, purged_reports, reason="淘汰"):
        if not purged_reports: return
        all_archived = []
        if os.path.exists(self.purged_alphas_archive_file):
            try:
                if os.path.getsize(self.purged_alphas_archive_file) > 1:
                    with open(self.purged_alphas_archive_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content: all_archived = json.loads(content)
                        if not isinstance(all_archived, list):
                             logger.warning(f"{self.purged_alphas_archive_file} 内容不是列表，将重置。")
                             all_archived = []
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析归档文件 {self.purged_alphas_archive_file}，将创建新文件。")
                all_archived = []
        for report in purged_reports:
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
        with self.hopeful_file_lock:
            existing_reports = []
            if os.path.exists(self.hopeful_alphas_file):
                try:
                    if os.path.getsize(self.hopeful_alphas_file) > 1:
                        with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if content: existing_reports = json.loads(content)
                            if not isinstance(existing_reports, list):
                                 logger.warning(f"{self.hopeful_alphas_file} 内容不是列表，将重置。")
                                 existing_reports = []
                except (IOError, json.JSONDecodeError):
                    logger.warning(f"无法解析 {self.hopeful_alphas_file}，将创建新的精华文件。")
                    existing_reports = []
            combined_reports = existing_reports + new_hopeful_reports
            purged_reports = []
            archived_reports = []
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
                self_corr_value = 0.0 # v7.9
                try:
                    checks_list = report.get('performance', {}).get('checks', [])
                    if isinstance(checks_list, list):
                        for check in checks_list:
                            if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                                self_corr_value = float(check.get('value', 0.0))
                                break
                except (ValueError, TypeError): pass
                is_self_corr_ok = self_corr_value < 0.7 
                is_high_quality = fitness_float > 0 and passed_count >= 4
                is_high_potential = fitness_float > -0.5 and passed_count >= 5
                if (is_high_quality or is_high_potential) and is_self_corr_ok:
                    purged_reports.append(report)
                elif (is_high_quality or is_high_potential) and not is_self_corr_ok:
                    logger.warning(f"策略 {report.get('expression', '')[:40]}... 因 Self-Correlation 过高 ({self_corr_value:.3f} > 0.7) 被精英池拒绝（即使 Fitness/Checks 达标）。")
                    archived_reports.append(report)
                elif any(isinstance(r, dict) and r.get('expression') == report.get('expression') for r in existing_reports):
                    archived_reports.append(report)
            
            logger.info(f"精英池清洗: {len(unique_reports_map)} -> {len(purged_reports)} (识别出 {len(archived_reports)} 个过时/高相关性策略)")
            self.archive_purged_alphas(archived_reports, reason="标准清洗 (含Self-Corr > 0.7)")
            
            # v9.0: 计算内部得分
            for report in purged_reports:
                report['internal_score'] = self._calculate_combined_score(report)
            purged_reports.sort(key=lambda x: x['internal_score'], reverse=True) # v9.0: 使用新key排序

            final_pool = purged_reports[:max_pool_size]
            if len(purged_reports) > max_pool_size:
                eliminated = purged_reports[max_pool_size:]
                logger.info(f"精英池末位淘汰: {len(purged_reports)} -> {len(final_pool)} (保留综合评分排名前 {max_pool_size} 的策略)")
                self.archive_purged_alphas(eliminated, reason="末位淘汰")
            try:
                with open(self.hopeful_alphas_file, 'w', encoding='utf-8') as f:
                    json.dump(final_pool, f, indent=4, ensure_ascii=False)
                logger.info(f"已将 {len(new_hopeful_reports)} 份新战报处理完毕，并完成了精英池的动态维护。当前池中共有 {len(final_pool)} 个策略。")
                self.hopeful_alphas_cache = final_pool
            except IOError as e:
                logger.error(f"保存精华战报文件时出错: {e}")

    # --- v7.8.3 修复: 消费者 (Worker) 线程 (保留) ---
    def _consumer_worker(self):
        """消费者工作线程，从队列中获取策略并执行测试。"""
        while True:
            strategy = None
            try:
                strategy = self.strategy_queue.get()
                if strategy is None:
                    self.strategy_queue.task_done()
                    break

                if not isinstance(strategy, dict) or 'expression' not in strategy:
                     logger.warning("从队列中获取到无效的 strategy 对象，已丢弃。")
                     self.strategy_queue.task_done()
                     continue

                idea_expr = strategy['expression']
                if not idea_expr:
                    logger.warning("从队列中获取到 expression 为空的 strategy 对象，已丢弃。")
                    self.strategy_queue.task_done()
                    continue

                logger.info(f"取得策略: {idea_expr[:60]}... (队列剩余: {self.strategy_queue.qsize()})")
                log_report = {"expression": idea_expr, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

                # 2. 执行 WQ 测试 (v9.0: self.wq 是已初始化的客户端)
                result = self.wq.test_alpha(idea_expr, strategy['settings'])

                # 3. 处理 WQ 429 Rate Limit
                if result == "RATE_LIMIT":
                    logger.warning(f"遭遇 WQ 429 (针对: {idea_expr})。")
                    
                    config = load_system_config() # v9.0: 使用导入的函数
                    current_wq_cooldown = config.get("wq_api_cooldown", 30)
                    logger.info(f"触发 {current_wq_cooldown}s 冷却... 策略将放回队列重试。")
                    self._enter_cooldown(reason="WorldQuant 429 Rate Limit")
                    time.sleep(current_wq_cooldown)

                    try:
                        self.strategy_queue.put(strategy)
                        logger.info(f"策略 {idea_expr[:60]}... 已放回队列。")
                    except queue.Full:
                         logger.error(f"尝试放回策略 {idea_expr[:60]}... 时队列已满！该策略将被丢弃。")
                         self.strategy_queue.task_done() # v7.8.3 修复
                         continue
                    
                    continue # v7.8.3 修复

                # 4. 处理 ERROR (黑名单逻辑)
                if isinstance(result, dict) and result.get("status") == "ERROR":
                    log_report["status"] = "ERROR"
                    self.log_tested_alphas([log_report])

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
                    passed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PASS") if isinstance(checks, list) else 0
                    failed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "FAIL") if isinstance(checks, list) else 0
                    pending_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PENDING") if isinstance(checks, list) else 0

                    fitness = is_stats.get('fitness', -999)
                    try: fitness_float = float(fitness)
                    except (ValueError, TypeError): fitness_float = -999

                    log_report["status"] = "COMPLETE"
                    log_report["fitness"] = fitness_float
                    log_report["passed_checks"] = passed_count
                    log_report["performance"] = is_stats # v7.9
                    self.log_tested_alphas([log_report])
                    checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                    self_corr_value = 0.0 # v7.9
                    try:
                        if isinstance(checks, list):
                            for check in checks:
                                if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                                    self_corr_value = float(check.get('value', 0.0))
                                    break
                    except (ValueError, TypeError): pass
                    is_self_corr_ok = self_corr_value < 0.7
                    
                    is_high_quality = fitness_float > 0 and passed_count >= 4
                    is_high_potential = fitness_float > -0.5 and passed_count >= 5

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
                        self.save_hopeful_reports([hopeful_report])
                    
                    elif (is_high_quality or is_high_potential) and not is_self_corr_ok:
                         logger.info(f"策略因 Self-Correlation 过高被拒绝。Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                    
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
                self.strategy_queue.task_done()
    # --- v7.8.3 结束 ---


    # --- v7.7 重构: 生产者 (Producer) 循环 (保留) ---
    def run(self, mode='discover', sleep_time=10):

        logger.info(f"Alpha 生成器启动 | 版本: {CURRENT_GENERATOR_VERSION} | 模式: {mode.upper()} | 并发 Workers: {self.concurrency_level} | 队列大小: {self.queue_max_size}")

        self.fields = self.wq.get_data_fields()
        self.operators = self.wq.get_operators()

        if self.operators == "RATE_LIMIT":
            logger.critical("获取操作符时遭遇 WorldQuant 429，触发冷却。")
            self._enter_cooldown(reason="WorldQuant 429 Rate Limit")

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
                # 1. 检查 LLM 冷却状态
                if time.time() < self._rate_limit_until:
                    remaining = self._rate_limit_until - time.time()
                    logger.info(f"[生产者] 当前处于冷却期。将在 {remaining/60:.1f} 分钟后恢复...")
                    time.sleep(min(remaining, 300))
                    continue

                # 2. 检查 WQ 字段/操作符
                self.fields = self.wq.get_data_fields()
                self.operators = self.wq.get_operators()
                if self.operators == "RATE_LIMIT":
                     logger.critical("[生产者] 获取操作符时遭遇 WorldQuant 429，触发冷却。")
                     self._enter_cooldown(reason="WorldQuant 429 Rate Limit")
                     continue
                if not self.fields or not self.operators:
                     logger.error("[生产者] 无法获取字段或操作符，将在60秒后重试。")
                     time.sleep(60)
                     continue

                # 3. (Evolve 模式) 更新种子和指导
                if mode == 'evolve':
                    evolution_seeds = self.load_evolution_seeds()
                    if not evolution_seeds:
                        mode = 'discover'
                        logger.warning("[生产者] 进化模式无法启动（无可用种子），已自动切换到发现模式。")
                    else:
                        strategic_guidance = self.analyze_successful_patterns()

                # 4. 检查队列是否已满
                if self.strategy_queue.qsize() >= self.queue_max_size:
                    logger.info(f"[生产者] 队列已满 ({self.strategy_queue.qsize()}/{self.queue_max_size})，暂停生成 10 秒...")
                    time.sleep(10)
                    continue

                logger.info(f"[生产者] [{mode.upper()}] 开始生成 1 个新 Alpha... (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")

                # 5. 生成新策略 (v9.0: 调用重构后的方法)
                idea = None
                if mode == 'discover':
                    idea = self.generate_alpha_idea(self.fields, self.operators, guidance=strategic_guidance)
                elif mode == 'evolve':
                    if not evolution_seeds:
                         logger.warning("[生产者] 进化模式种子列表为空，跳过本轮生成。")
                         time.sleep(sleep_time)
                         continue
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance)

                # 6. 预检
                if isinstance(idea, dict) and idea.get("expression"):
                    expr = idea.get("expression")
                    with self.tested_alphas_lock:
                        is_tested = expr in self.tested_alphas
                    if is_tested:
                        logger.info(f"[生产者] 策略 {expr[:60]}... 已被测试过，丢弃。")
                        continue
                    if is_alpha_syntactically_suspicious(expr):
                        continue
                    if self.is_using_blacklisted_identifier(expr):
                        continue

                    # 7. 放入队列
                    try:
                         self.strategy_queue.put(idea)
                         logger.info(f"[生产者] 新策略已生成并通过预检，放入队列。 (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")
                    except queue.Full:
                         logger.error("[生产者] 尝试放入策略时队列已满！")

                elif idea is None:
                     # idea 为 None 是正常情况 (LLM 没返回, 或触发了冷却)
                     logger.warning("[生产者] LLM未能生成有效的 Alpha 策略 (或已进入冷却)。")
                
                # else: idea == "RATE_LIMIT" (已被 generate_... 方法处理, idea 会是 None)

                logger.info(f"[生产者] 本轮生成结束。等待{sleep_time}秒开始下一轮...")
                time.sleep(sleep_time)

            except Exception as e:
                logger.critical(f"[生产者] 循环发生致命错误: {e}", exc_info=True)
                time.sleep(60)
# --- v7.7 结束 ---


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator v9.0 (Modularized)')
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle (v7.7: 已弃用，但保留)")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    
    # --- v8.0: 移除静态参数 (保留) ---
    
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve'], help="Generation mode")
    parser.add_argument('--log-file', type=str, default='alpha_generator.log', help="Name of the log file in the logs directory")
    args = parser.parse_args()

    # --- v9.0: 使用导入的函数 ---
    setup_logging(args.log_file)
    
    logger.info(f"正在加载 {args.api_config_path} (用于 LLM) 和 system_config.json (用于服务)...")
    config = load_system_config()
    # --- v9.0 结束 ---
    
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
            # --- v9.0: WQ 客户端来自导入的类 ---
            wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
            logger.info("WorldQuant 客户端初始化成功。")
            retry_count = 0
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"初始化 WorldQuant 客户端时检测到 429 Rate Limit: {e}。")
                
                # --- v9.0: 使用导入的函数 ---
                config_init = load_system_config()
                wq_cooldown_init = config_init.get("wq_api_cooldown", 30)
                logger.warning(f"将进入 {wq_cooldown_init} 秒冷却期...")
                time.sleep(wq_cooldown_init)
                # --- v9.0 结束 ---

                retry_count = 0
                continue
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
        generator = AlphaGenerator(wq=wq_client, # v9.0: 传入 wq_client
                                 api_config_path=args.api_config_path,
                                 batch_size=args.batch_size,
                                 concurrency_level=concurrency)

        # v8.0: 使用从配置中读取的静态参数
        generator.run(mode=args.mode, sleep_time=sleep_time)

    except Exception as e:
        logger.critical(f"生成器运行时发生致命错误: {e}", exc_info=True)