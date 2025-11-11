# --- alpha_generator_ollama.py v13.3.13 (修复生产者 I/O 风暴) ---
import argparse
import logging
import json
import os
import time
import requests 
import random
from datetime import datetime, timezone 
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import queue

# --- v13.3.5: 强制使用 utils 中的安全函数 ---
# 导入我们 v13.3.5 版的、带保险的 utils
import utils 
# --- v13.3.5: 结束 ---

from wq_client import WorldQuant
from llm_provider import LLMProvider

# --- v13.3.13: 版本号 ---
CURRENT_GENERATOR_VERSION = "v13.3.13 (Producer I/O Storm Fix)"
# --- v13.3.13: 结束 ---

logger = logging.getLogger(__name__)

INVALID_FUNCTIONS_FILE = "invalid_functions.json"
BLACKLIST_MAX_STRIKES = 3 

SUBMISSION_FAILURE_LOG_FILE = "submission_failure_log.json"


def is_alpha_syntactically_suspicious(alpha_code: str) -> bool:
    ts_functions_pattern = r'ts_([a-zA-Z_]+)\(([^,)]+)\)'
    match = re.search(ts_functions_pattern, alpha_code)
    if match:
        params = match.group(2).split(',')
        if len(params) == 1 and not params[0].strip().isdigit():
            logger.warning(f"本地预检失败: Alpha '{alpha_code}' 中的函数 '{match.group(0)}' 可能缺少 lookback 参数。已拒绝。")
            return True
    return False

class AlphaGenerator:
    def __init__(self, wq: WorldQuant, api_config_path, batch_size=5, concurrency_level=2): 
        self.wq = wq 
        self.batch_size = batch_size
        
        try:
            self.llm = LLMProvider(api_config_path=api_config_path)
        except Exception as e:
            logger.critical(f"初始化 LLMProvider 失败: {e}")
            raise

        # --- v13.3.5: 移除不安全的本地路径和锁 (已集中到 utils.py) ---
        # self.hopeful_alphas_file = "hopeful_alphas.json" # <--- 移除
        # self.hopeful_file_lock = threading.Lock() # <--- 移除
        # --- v13.3.5: 结束 ---

        self.tested_alphas_logfile = "tested_alphas_log.json"
        self.purged_alphas_archive_file = "purged_alphas_archive.json"
        
        self.submission_failure_log_file = SUBMISSION_FAILURE_LOG_FILE

        self.tested_alphas_lock = threading.Lock()
        self.blacklist_lock = threading.Lock()
        self.failure_log_lock = threading.Lock()

        self.tested_alphas = self.load_tested_alphas()
        self.hopeful_alphas_cache = []
        
        self.submission_failures_cache = [] 
        self.submission_failures_set = set() 
        self.submission_failures_map = {} 
        self.load_submission_failures() 

        self.invalid_functions_file = INVALID_FUNCTIONS_FILE
        self.blacklist_counts = self.load_blacklist_counts()
        self.blacklist_max_strikes = BLACKLIST_MAX_STRIKES
        self.identifier_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z_0-9]*)\b')
        self.fields = [] 

        self.concurrency_level = concurrency_level
        self.queue_max_size = self.concurrency_level * 2
        self.strategy_queue = queue.Queue(maxsize=self.queue_max_size)
        self.consumer_threads = []
        # --- v13.3.15: 添加日志合并状态 ---
        self._producer_paused_logging_state = False 
        # --- v13.3.15 结束 ---

    def _mutate_settings(self, settings_dict: dict) -> dict:
        try:
            # v13.3.5: 使用 utils 的安全函数
            config = utils.load_system_config()
            search_space = config.get("evolver_search_space")
            
            if not search_space or not isinstance(search_space, dict):
                logger.warning("[Mutate] 未在 system_config.json 中找到 evolver_search_space，跳过设置突变。")
                return settings_dict

            num_mutations = random.choices([2, 3, 4], weights=[0.3, 0.5, 0.2], k=1)[0]
            
            available_keys = [key for key in search_space if key in self.wq.default_settings and search_space[key]]
            
            if not available_keys:
                logger.warning("[Mutate] evolver_search_space 中没有可用于突变的键，跳过。")
                return settings_dict

            keys_to_mutate = random.sample(available_keys, min(num_mutations, len(available_keys)))
            mutated_settings = settings_dict.copy()
            log_msgs = []

            for key in keys_to_mutate:
                old_value = mutated_settings.get(key) 
                possible_new_values = [v for v in search_space[key] if v != old_value] 
                
                if not possible_new_values: continue 

                new_value = random.choice(possible_new_values)
                mutated_settings[key] = new_value
                log_msgs.append(f"{key}: {old_value} -> {new_value}")

            if log_msgs:
                logger.info(f"[Evolver Mutate] 突变了 {len(log_msgs)} 个设置: {', '.join(log_msgs)}")
            
            return mutated_settings

        except Exception as e:
            logger.error(f"[Mutate] 设置突变时发生错误: {e}", exc_info=True)
            return settings_dict 

    def load_tested_alphas(self):
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

    def load_submission_failures(self):
        """ (v12.0) 从 submission_failure_log.json 加载“地面真相”失败数据。 """
        filepath = self.submission_failure_log_file
        with self.failure_log_lock:
            if not os.path.exists(filepath):
                logger.warning(f"[Failure Log] 未找到失败日志: {filepath}。反馈循环将无法获取失败案例。")
                self.submission_failures_cache = []
                self.submission_failures_set = set()
                self.submission_failures_map = {}
                return

            try:
                if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2:
                    self.submission_failures_cache = []
                    self.submission_failures_set = set()
                    self.submission_failures_map = {}
                    return
                
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if isinstance(data, list):
                    self.submission_failures_cache = data
                    new_set = set()
                    new_map = {}
                    for item in data:
                        if isinstance(item, dict) and 'expression' in item:
                            expr = item['expression']
                            new_set.add(expr)
                            new_map[expr] = item.get('reason', 'UNKNOWN')
                    if new_set != self.submission_failures_set:
                        new_failures = len(new_set - self.submission_failures_set)
                        if new_failures > 0:
                            logger.info(f"[Failure Log] 成功加载并刷新失败日志。检测到 {new_failures} 个新失败案例，总计 {len(new_set)} 个。")
                        else:
                            logger.info(f"[Failure Log] 成功加载失败日志。总计 {len(new_set)} 个 (无变化)。")
                        self.submission_failures_set = new_set
                        self.submission_failures_map = new_map
                else:
                    logger.warning(f"[Failure Log] 文件 {filepath} 格式不正确（不是列表），已忽略。")
            except Exception as e:
                logger.error(f"[Failure Log] 加载 {filepath} 时出错: {e}", exc_info=False)

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
            current_counts = self.load_blacklist_counts() 
            current_count = current_counts.get(identifier_name, 0)
            current_count += 1
            current_counts[identifier_name] = current_count
            try:
                with open(self.invalid_functions_file, 'w', encoding='utf-8') as f:
                    json.dump(current_counts, f, indent=4)
                self.blacklist_counts = current_counts 
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
        
        # v13.3.5: 使用安全加载函数来填充缓存
        self.hopeful_alphas_cache = utils.load_hopeful_alphas_safe()
        hopeful_expressions = {alpha.get('expression') for alpha in self.hopeful_alphas_cache}

        potential_pearls = []
        for record in sample_records:
            if not isinstance(record, dict): continue
            expr = record.get('expression')
            if not expr or record.get('status') != 'COMPLETE' or expr in hopeful_expressions or expr in self.submission_failures_set:
                continue
            passed_count = record.get('passed_checks', 0)
            fitness = record.get('fitness', -999)
            if passed_count == 3 and fitness > -1.0:
                record['potential_score'] = self._calculate_potential_score(record) 
                potential_pearls.append(record)

        if not potential_pearls: return None
        potential_pearls.sort(key=lambda x: x.get('potential_score', -999), reverse=True)
        best_pearl = potential_pearls[0]
        logger.info(f"考古学家在 {len(sample_records)} 条记录中发现一颗遗珠！(已过滤失败日志) 潜力分: {best_pearl['potential_score']:.3f}, Expression: {best_pearl['expression']}")
        return {"expression": best_pearl['expression'], "performance": best_pearl.get('performance', {})}

    def _get_self_correlation(self, report_or_record) -> float:
        if not isinstance(report_or_record, dict): return 0.0
        perf = report_or_record.get('performance', {})
        if not isinstance(perf, dict): return 0.0
        
        self_corr_value = 0.0
        try:
            checks_list = perf.get('checks', [])
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation':
                        self_corr_value = float(check.get('value', 0.0))
                        break
        except (ValueError, TypeError): pass
        return self_corr_value

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
        
        self_corr_value = self._get_self_correlation(report) 
        
        try: fitness_f = float(fitness)
        except (ValueError, TypeError): fitness_f = -999
        try: sharpe_f = float(sharpe)
        except (ValueError, TypeError): sharpe_f = 0.0
        try: turnover_f = float(turnover)
        except (ValueError, TypeError): turnover_f = 1.0
        
        self_corr_penalty = 0.0 
        if self_corr_value > 0.7:
            self_corr_penalty = (self_corr_value - 0.7) * 5.0
        
        if report.get('expression') in self.submission_failures_set:
            self_corr_penalty += 10.0 
            
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
            
            self_corr_value = self._get_self_correlation(record) 
            
            self_corr_penalty = 0.0 
            if self_corr_value > 0.7:
                self_corr_penalty = (self_corr_value - 0.7) * 5.0
            
            if record.get('expression') in self.submission_failures_set:
                return -999.0 
                
            score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
            return score
        except (ValueError, TypeError):
            return -999

    # --- v13.3.5: 修复 load_evolution_seeds (使用安全加载) ---
    def load_evolution_seeds(self, total_sample_size=20, wild_card_count=5):
        seeds = []
        
        # --- v13.3.5: 关键修复 ---
        # 移除不安全的本地文件锁和 open()
        try:
            # 使用 utils 的安全函数，它包含重试和跨进程锁
            self.hopeful_alphas_cache = utils.load_hopeful_alphas_safe()
            seeds.extend(self.hopeful_alphas_cache)
            logger.info(f"已加载 {len(self.hopeful_alphas_cache)} 个精英策略。")
        except Exception as e:
             logger.error(f"加载精英池 {utils.HOPEFUL_ALPHAS_FILE} 失败: {e}", exc_info=True)
             self.hopeful_alphas_cache = []
        # --- v13.3.5: 结束 ---

        pearl = self.excavate_one_pearl()
        if pearl:
            seeds.append(pearl)
        if not seeds:
            logger.warning("精英池为空，且未挖掘到遗珠，无法获取进化种子。")
            return []

        for seed in seeds:
            seed['internal_score'] = self._calculate_combined_score(seed)
            
        seeds.sort(key=lambda x: x['internal_score'], reverse=True) 
        
        original_seed_count = len(seeds)
        seeds = [s for s in seeds if s['internal_score'] > -10.0]
        filtered_count = original_seed_count - len(seeds)
        if filtered_count > 0:
            logger.info(f"[Evolve Seeds] 已从种子池中过滤掉 {filtered_count} 个已知失败的策略。")

        
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
        
        # v13.3.5: 这里的 hopeful_alphas_cache 已经是安全加载的
        logger.info(f"策略导师将从 {len(self.hopeful_alphas_cache)} 个精英策略中学习模式。")
        logger.info(f"已抽取 {len(evolution_seeds)} 个种子 (目标: {elite_count} 精英, {wild_card_count} 外卡 => 实际: {k_elite} 精英, {k_wild} 外卡) 作为本轮进化父本。")
        return evolution_seeds
    # --- v13.3.5: 结束 ---

    def analyze_successful_patterns(self, top_k_pool=20, sample_size=7):
        # v13.3.5: hopeful_alphas_cache 在 load_evolution_seeds 中被安全填充
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

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        result = self.llm.generate_alpha_idea(fields, operators, guidance, failed_examples)
        
        if result == "BUDGET_EXHAUSTED":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 BUDGET_EXHAUSTED (Discover)。")
            return "BUDGET_EXHAUSTED"
        
        if result == "RATE_LIMIT":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 RATE_LIMIT (Discover)。")
            return "RATE_LIMIT"
        
        return result

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        if 'performance' not in base_alpha_obj or not base_alpha_obj['performance']:
             base_alpha_obj['performance'] = {}
        parent_settings = base_alpha_obj['performance'].get('settings', self.wq.default_settings)
        if not parent_settings or not isinstance(parent_settings, dict):
             parent_settings = self.wq.default_settings
        base_alpha_obj['performance']['settings'] = parent_settings

        parent_expression = base_alpha_obj.get('expression')
        special_guidance = None
        
        if parent_expression in self.submission_failures_set:
            failure_reason = self.submission_failures_map.get(parent_expression, 'UNKNOWN')
            logger.critical(f"[Evolve Guidance] 父本在'失败日志'中 (原因: {failure_reason})！强制“结构性突变”。")
            special_guidance = f"**CRITICAL MUTATION REQUIRED!** Parent is a KNOWN FAILURE (Reason: {failure_reason}). DO NOT micro-optimize. You MUST perform a major structural change (e.g., add new operators, change logic) to escape this failed pattern."
        else:
            parent_self_corr = self._get_self_correlation(base_alpha_obj)
            if parent_self_corr > 0.7:
                logger.warning(f"[Evolve Guidance] 父本 {parent_expression[:30]}... 模拟 Self-Corr 高 ({parent_self_corr:.3f})，添加特殊指导。")
                special_guidance = f"**PRIORITY: Reduce Self-Correlation!** Parent's simulated correlation is too high ({parent_self_corr:.3f}). Make significant changes to lower it."
        
        if special_guidance:
            base_alpha_obj['_special_guidance_high_corr'] = special_guidance
        else:
            base_alpha_obj.pop('_special_guidance_high_corr', None)

        result = self.llm.generate_evolved_alpha_idea(base_alpha_obj, guidance=guidance) 
        base_alpha_obj.pop('_special_guidance_high_corr', None)

        if result == "BUDGET_EXHAUSTED":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 BUDGET_EXHAUSTED (Evolve)。")
            return "BUDGET_EXHAUSTED"

        if result == "RATE_LIMIT":
            logger.warning("[AlphaGenerator] 收到来自 LLMProvider 的 RATE_LIMIT (Evolve)。")
            return "RATE_LIMIT"
        
        if not isinstance(result, dict) or not result.get('expression'):
            logger.warning("[AlphaGenerator] LLM 未能返回有效的进化 Expression 字典。")
            return None 

        mutated_settings = self._mutate_settings(parent_settings)
        
        evolved_strategy = {
            "expression": result['expression'], 
            "settings": mutated_settings         
        }
        return evolved_strategy

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

    # --- v13.3.5: 修复 save_hopeful_reports (使用安全加载/保存) ---
    def save_hopeful_reports(self, new_hopeful_reports, max_pool_size=None):
        """
        v13.3.5: 完全重构，使用 utils 中的安全函数 (load/save)
                 来防止数据丢失和跨进程写入冲突。
        """
        
        # --- v13.2.0: 动态精英池上限 ---
        if max_pool_size is None:
            try:
                config = utils.load_system_config()
                max_pool_size = int(config.get("hopeful_pool_max_size", 200)) # 从配置读取
            except Exception as e:
                logger.error(f"[Hopeful Save] 无法从 system_config 读取 hopeful_pool_max_size: {e}。回退到 200。")
                max_pool_size = 200 # 紧急回退
        # --- v13.2.0: 结束 ---
        
        # --- v13.3.5: 关键修复 (安全加载) ---
        # 1. 使用 utils 的安全函数加载，它包含重试和跨进程锁
        existing_reports = utils.load_hopeful_alphas_safe()
        # --- v13.3.5: 结束 ---
        
        combined_reports = existing_reports + new_hopeful_reports
        purged_reports = []
        archived_reports = []
        unique_reports_map = {}
        for report in combined_reports:
            if isinstance(report, dict) and 'expression' in report:
                 unique_reports_map[report.get('expression')] = report
        
        logger.info(f"[Hopeful Save] 开始精英池清洗... (传入 {len(new_hopeful_reports)} / 现有 {len(existing_reports)} / 独特 {len(unique_reports_map)})")
        expressions_to_archive = set()
        
        for expr in unique_reports_map:
            # 规则1: 如果在“地面真相”失败日志中，必须淘汰
            if expr in self.submission_failures_set:
                logger.warning(f"策略 {expr[:40]}... 因存在于'失败日志'中，被精英池拒绝。")
                expressions_to_archive.add(expr)
                continue

            report = unique_reports_map[expr]
            fitness = report.get('performance', {}).get('fitness', -999)
            checks_summary = report.get('checks_summary', '0 PASS')
            passed_count = 0
            try:
                match = re.search(r'(\d+)\s+PASS', checks_summary or '')
                if match: passed_count = int(match.group(1))
            except (ValueError, TypeError): pass
            try: fitness_float = float(fitness)
            except (ValueError, TypeError): fitness_float = -999
            
            self_corr_value = self._get_self_correlation(report) 
            is_self_corr_ok = self_corr_value < 0.7 
            
            is_high_quality = fitness_float > 0 and passed_count >= 4
            is_high_potential = fitness_float > -0.5 and passed_count >= 5
            
            # 规则2: 如果模拟 Self-Corr 过高，淘汰
            if (is_high_quality or is_high_potential) and not is_self_corr_ok:
                logger.warning(f"策略 {expr[:40]}... 因模拟 Self-Correlation 过高 ({self_corr_value:.3f} > 0.7) 被精英池拒绝（即使 Fitness/Checks 达标）。")
                expressions_to_archive.add(expr)
                continue
            
            # 规则3: 如果不满足质量标准，淘汰
            if not (is_high_quality or is_high_potential):
                expressions_to_archive.add(expr)
                continue

            # 如果通过所有检查，则保留
            purged_reports.append(report)
        
        # 将所有被标记为淘汰的策略移入归档列表
        for expr in expressions_to_archive:
            if expr in unique_reports_map:
                archived_reports.append(unique_reports_map[expr])

        logger.info(f"精英池清洗: {len(unique_reports_map)} -> {len(purged_reports)} (识别出 {len(archived_reports)} 个过时/高相关性/已知失败策略)")
        self.archive_purged_alphas(archived_reports, reason="标准清洗 (含Self-Corr > 0.7 或 Ground Truth Failure)")
        
        
        for report in purged_reports:
            report['internal_score'] = self._calculate_combined_score(report)
        purged_reports.sort(key=lambda x: x['internal_score'], reverse=True) 

        # (动态 max_pool_size 逻辑保持不变)
        final_pool = purged_reports[:max_pool_size] 
        if len(purged_reports) > max_pool_size:
            eliminated = purged_reports[max_pool_size:]
            logger.info(f"精英池末位淘汰: {len(purged_reports)} -> {len(final_pool)} (保留综合评分排名前 {max_pool_size} 的策略)")
            self.archive_purged_alphas(eliminated, reason="末位淘汰")
            
        # --- v13.3.5: 关键修复 (安全保存) ---
        # 3. 使用 utils 的安全函数保存，它包含跨进程锁
        try:
            if utils.save_hopeful_alphas_safe(final_pool):
                logger.info(f"已将 {len(new_hopeful_reports)} 份新战报处理完毕，并完成了精英池的动态维护。当前池中共有 {len(final_pool)} 个策略。")
                self.hopeful_alphas_cache = final_pool
            else:
                logger.error(f"保存精华战报文件时出错 (utils.save_hopeful_alphas_safe 返回 False)。")
        except Exception as e:
            logger.error(f"保存精华战报文件时发生意外错误: {e}", exc_info=True)
        # --- v13.3.5: 结束 ---
    # --- v13.3.5: 结束 ---

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

                result = self.wq.test_alpha(idea_expr, strategy['settings'])

                if result == "RATE_LIMIT":
                    # --- v13.3.11 Livelock (锁死) 修复 ---
                    logger.warning(f"遭遇 WQ 429 (针对: {idea_expr})。")
                    logger.warning(f"[Worker] 看门狗 B (wq_client) 已激活。此策略 {idea_expr[:60]}... 将被丢弃 (不再放回队列)。")
                    
                    # 关键修复：我们不再将 'strategy' 放回队列。
                    # wq_client 已经在 system_config.json 中设置了全局冷却时间戳。
                    # 所有 *其他* worker 在下次调用 _acquire_wq_token 时会自动休眠，
                    # 从而优雅地暂停整个系统，而不是被这个任务卡死。
                    
                    # 我们仍然需要标记此任务 "完成"，以释放 worker 去做别的任务。
                    self.strategy_queue.task_done()
                    continue 
                    # --- 修复结束 ---

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
                        error_type = match.group(1); bad_identifier = match.group(2); should_blacklist = False
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
                    self.strategy_queue.task_done() 
                    continue 

                if result in ["TIMEOUT"]:
                    log_report["status"] = result
                    self.log_tested_alphas([log_report])
                    logger.warning(f"Alpha 模拟{result}，已记录并丢弃 (不计入黑名单): {idea_expr}")
                    self.strategy_queue.task_done() 
                    continue 

                if isinstance(result, dict):
                    is_stats = result.get("is", {}); alpha_id = result.get("id")
                    if not isinstance(is_stats, dict) or not alpha_id:
                        logger.warning(f"模拟返回不完整 (缺少 'is' 或 'id')，已丢弃: {idea_expr}")
                        self.strategy_queue.task_done()
                        continue 

                    checks = is_stats.get("checks", [])
                    passed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PASS") if isinstance(checks, list) else 0
                    failed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "FAIL") if isinstance(checks, list) else 0
                    pending_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PENDING") if isinstance(checks, list) else 0

                    fitness = is_stats.get('fitness', -999)
                    try: fitness_float = float(fitness)
                    except (ValueError, TypeError): fitness_float = -999

                    log_report["status"] = "COMPLETE"; log_report["fitness"] = fitness_float
                    log_report["passed_checks"] = passed_count; log_report["performance"] = is_stats 
                    log_report["performance"]["settings"] = strategy.get("settings", self.wq.default_settings)
                    self.log_tested_alphas([log_report])
                    checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                    self_corr_value = self._get_self_correlation(log_report)
                    is_self_corr_ok = self_corr_value < 0.7
                    
                    if idea_expr in self.submission_failures_set:
                        logger.warning(f"策略 {idea_expr[:40]}... 因存在于'失败日志'中，被拒绝（即使模拟通过）。")
                        self.strategy_queue.task_done()
                        continue
                    
                    is_high_quality = fitness_float > 0 and passed_count >= 4
                    is_high_potential = fitness_float > -0.5 and passed_count >= 5

                    if (is_high_quality or is_high_potential) and is_self_corr_ok:
                        if is_high_potential and not is_high_quality:
                            logger.info(f"发现一个高潜力策略 (Fitness < 0, 但 Checks >= 5)，破格录用！ Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                        else:
                            logger.info(f"发现一个高质量策略！ Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")

                        regular_code = result.get("regular", {}).get("code") if isinstance(result.get("regular"), dict) else None
                        hopeful_report = {
                            "expression": regular_code or idea_expr, "alpha_id": alpha_id,
                            "result_url": f"https://platform.worldquantbrain.com/alphas/regular/{alpha_id}",
                            "grade": result.get("grade", "UNKNOWN"), "timestamp": log_report["timestamp"],
                            "performance": log_report["performance"], "checks_summary": checks_summary
                        }
                        perf_items = is_stats.items()
                        stats_str = ", ".join([f"{key}: {value:.3f}" for key, value in perf_items if isinstance(value, (int, float))])
                        logger.info(f"生成高质量策略战报 [{checks_summary}] -> {stats_str}")
                        
                        # --- v13.3.5: 调用点 ---
                        # (现在调用的是 v13.3.5 修复后的安全函数)
                        self.save_hopeful_reports([hopeful_report])
                        # --- v13.3.5: 结束 ---
                    
                    elif (is_high_quality or is_high_potential) and not is_self_corr_ok:
                         logger.info(f"策略因 Self-Correlation 过高被拒绝。Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                    else:
                        logger.info(f"策略未达到高质量标准，已丢弃。Fitness: {fitness_float:.3f}, Checks: {passed_count} PASS, Self-Corr: {self_corr_value:.3f}. Alpha: {idea_expr}")
                else:
                     logger.error(f"收到未知的模拟结果类型: {type(result)} for alpha: {idea_expr}")

            except Exception as exc:
                expr_for_log = "UNKNOWN"
                if isinstance(strategy, dict) and 'expression' in strategy: expr_for_log = strategy['expression']
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


    def run(self, mode='discover'):
        logger.info(f"Alpha 生成器启动 | 版本: {CURRENT_GENERATOR_VERSION} | 模式: {mode.upper()} | 并发 Workers: {self.concurrency_level} | 队列大小: {self.queue_max_size}")

        self.fields = self.wq.get_data_fields()
        self.operators = self.wq.get_operators()

        if self.operators == "RATE_LIMIT":
            logger.critical("获取操作符时遭遇 WorldQuant 429。看门狗 B 将在下次调用时处理。")
        if not self.fields or not self.operators or self.operators == "RATE_LIMIT":
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            if self.operators != "RATE_LIMIT": time.sleep(60)

        logger.info(f"正在启动 {self.concurrency_level} 个消费者 (worker) 线程...")
        for i in range(self.concurrency_level):
            t = threading.Thread(target=self._consumer_worker, name=f"Worker-{i+1}", daemon=True)
            t.start()
            self.consumer_threads.append(t)

        evolution_seeds = []; strategic_guidance = []; failed_examples_for_miner = [] 

        while True:
            try:
                # --- v13.3.15: 修复生产者日志刷屏 ---
                if self.strategy_queue.qsize() >= self.queue_max_size:
                    config = utils.load_system_config()
                    queue_sleep = config.get("producer_queue_full_sleep", 10) 
                    
                    # 仅在状态 *首次* 变为“暂停”时记录
                    if not self._producer_paused_logging_state:
                        logger.info(f"[生产者] 队列已满 ({self.strategy_queue.qsize()}/{self.queue_max_size})。生产者将暂停，直到队列出现空位... (此消息将合并)")
                        self._producer_paused_logging_state = True # 设置状态为“已暂停”
                    
                    time.sleep(queue_sleep) 
                    continue # <--- 关键：跳过本轮循环
                
                # 如果代码运行到这里，说明队列 *未* 满。
                # 检查是否需要记录“恢复”日志
                if self._producer_paused_logging_state:
                    logger.info(f"[生产者] 队列出现空位 ({self.strategy_queue.qsize()}/{self.queue_max_size})。恢复刷新 fields/operators 并生成...")
                    self._producer_paused_logging_state = False # 重置状态
                # --- v13.3.15 修复结束 ---

                # (v13.3.15: 移除旧的 "队列未满..." 日志)
                self.fields = self.wq.get_data_fields() 
                self.operators = self.wq.get_operators()
                
                if self.operators == "RATE_LIMIT":
                     logger.critical("[生产者] 获取操作符时遭遇 WorldQuant 429。看门狗 B 将在下次调用时处理。")
                     
                if not self.fields or not self.operators:
                     logger.error("[生产者] 无法获取字段或操作符，将在60秒后重试。")
                     time.sleep(60)
                     continue

                self.load_submission_failures()

                if mode == 'evolve':
                    # v13.3.5: 此函数现在是安全的
                    evolution_seeds = self.load_evolution_seeds()
                    if not evolution_seeds:
                        mode = 'discover'
                        logger.warning("[生产者] 进化模式无法启动（无可用种子），已自动切换到发现模式。")
                    else:
                        strategic_guidance = self.analyze_successful_patterns()
                
                if mode == 'discover':
                    failed_examples_for_miner = [
                        item['expression'] for item in self.submission_failures_cache
                        if isinstance(item, dict) and 'HIGH_SELF_CORR' in item.get('reason', '').upper()
                    ]
                    if failed_examples_for_miner:
                        failed_examples_for_miner = failed_examples_for_miner[-20:]
                
                # (v13.3.13: 旧的队列检查 [line 841] 已被移到顶部)

                logger.info(f"[生产者] [{mode.upper()}] 开始生成 1 个新 Alpha... (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")

                idea = None
                if mode == 'discover':
                    idea = self.generate_alpha_idea( self.fields, self.operators, guidance=strategic_guidance, failed_examples=failed_examples_for_miner )
                elif mode == 'evolve':
                    if not evolution_seeds:
                         logger.warning("[生产者] 进化模式种子列表为空，跳过本轮生成。")
                         time.sleep(1) 
                         continue
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance) 

                if idea == "BUDGET_EXHAUSTED":
                    logger.critical(f"[生产者] 看门狗 A: LLM 预算已用尽。生产者线程将休眠 15 分钟...")
                    time.sleep(900) 
                    continue 
                
                if idea == "RATE_LIMIT":
                    logger.warning(f"[生产者] LLM API 速率限制 (非预算问题)。生产者线程将休眠 5 分钟...")
                    time.sleep(300) 
                    continue 

                if isinstance(idea, dict) and idea.get("expression"):
                    expr = idea.get("expression")
                    with self.tested_alphas_lock:
                        is_tested = expr in self.tested_alphas
                    is_known_failure = expr in self.submission_failures_set
                    if is_tested:
                        logger.info(f"[生产者] 策略 {expr[:60]}... 已被测试过，丢弃。")
                        continue
                    if is_known_failure:
                        logger.warning(f"[生产者] 策略 {expr[:60]}... 已在'失败日志'中，丢弃。")
                        continue
                    if is_alpha_syntactically_suspicious(expr):
                        continue
                    if self.is_using_blacklisted_identifier(expr):
                        continue
                    try:
                         self.strategy_queue.put(idea)
                         logger.info(f"[生产者] 新策略已生成并通过预检，放入队列。 (队列: {self.strategy_queue.qsize()}/{self.queue_max_size})")
                    except queue.Full:
                         logger.error("[生产者] 尝试放入策略时队列已满！")
                elif idea is None:
                     logger.warning("[生产者] LLM未能生成有效的 Alpha 策略。")
            except Exception as e:
                logger.critical(f"[生产者] 循环发生致命错误: {e}", exc_info=True)
                time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator v13.3.13 (Producer I/O Storm Fix)') # v13.3.13
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle (v7.7: 已弃用，但保留)")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve'], help="Generation mode")
    parser.add_argument('--log-file', type=str, default='alpha_generator.log', help="Name of the log file in the logs directory")
    args = parser.parse_args()

    # v13.3.5: 确保 utils.setup_logging 被调用
    utils.setup_logging(args.log_file)
    
    logger.info(f"正在加载 {args.api_config_path} (用于 LLM) 和 system_config.json (用于服务)...")
    # v13.3.5: 使用 utils 的安全函数
    config = utils.load_system_config()
    
    if args.mode == 'discover':
        concurrency = config.get("miner_concurrency", 1)
        logger.info(f"[v13.0 Config] 启动 Miner (discover) 模式: Concurrency={concurrency}")
    elif args.mode == 'evolve':
        concurrency = config.get("evolver_concurrency", 1)
        logger.info(f"[v13.0 Config] 启动 Evolver (evolve) 模式: Concurrency={concurrency}")
    else:
        logger.error(f"未知的模式: {args.mode}。使用默认值 1。")
        concurrency = 1

    MAX_INIT_RETRIES = 5; SHORT_SLEEP = 30; LONG_SLEEP = 300
    retry_count = 0; wq_client = None

    while wq_client is None:
        try:
            wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
            logger.info("WorldQuant 客户端初始化成功。")
            retry_count = 0
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"初始化 WorldQuant 客户端时检测到 429 Rate Limit: {e}。")
                logger.warning(f"[Watchdog B] 看门狗 B 已激活。将在下次重试时自动处理冷却...")
                # v13.3.5: 使用 utils 的安全函数
                config_init = utils.load_system_config()
                wq_cooldown_init = config_init.get("wq_api_limiter", {}).get("wq_429_cooldown_seconds", 60)
                logger.warning(f"将进入 {wq_cooldown_init} 秒冷却期...")
                time.sleep(wq_cooldown_init)
                retry_count = 0
                continue

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
        generator = AlphaGenerator(wq=wq_client, 
                                 api_config_path=args.api_config_path,
                                 batch_size=args.batch_size,
                                 concurrency_level=concurrency)
        generator.run(mode=args.mode)
    except Exception as e:
        logger.critical(f"生成器运行时发生致命错误: {e}", exc_info=True)