# --- alpha_generator_ollama.py v13.3.16 (Dynamic Sampling) ---
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
from filelock import FileLock 
import utils 
from wq_client import WorldQuant
from llm_provider import LLMProvider       

CURRENT_GENERATOR_VERSION = "v13.3.16 (Dynamic Sampling)"
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
        except Exception as e: logger.critical(f"初始化 LLMProvider 失败: {e}"); raise
        self.tested_alphas_logfile = "tested_alphas_log.json"
        self.purged_alphas_archive_file = "purged_alphas_archive.json"
        self.submission_failure_log_file = SUBMISSION_FAILURE_LOG_FILE
        self.tested_alphas_lock = threading.Lock()
        self.blacklist_lock = threading.Lock()
        self.failure_log_lock = threading.Lock()
        self.tested_alphas = self.load_tested_alphas()
        self.hopeful_alphas_cache = []
        self.submission_failures_cache = []; self.submission_failures_set = set(); self.submission_failures_map = {}
        self.load_submission_failures() 
        self.invalid_functions_file = INVALID_FUNCTIONS_FILE
        self.invalid_functions_lock_file = "invalid_functions.json.lock"
        self.blacklist_file_lock = FileLock(self.invalid_functions_lock_file, timeout=10)
        self.blacklist_counts = self.load_blacklist_counts()
        self.blacklist_max_strikes = BLACKLIST_MAX_STRIKES
        self.identifier_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z_0-9]*)\b')
        self.fields = []; self.concurrency_level = concurrency_level
        self.queue_max_size = self.concurrency_level * 2
        self.strategy_queue = queue.Queue(maxsize=self.queue_max_size)
        self.consumer_threads = []
        self._producer_paused_logging_state = False 

    def _mutate_settings(self, settings_dict: dict) -> dict:
        try:
            config = utils.load_system_config()
            search_space = config.get("evolver_search_space")
            if not search_space or not isinstance(search_space, dict): return settings_dict
            num_mutations = random.choices([2, 3, 4], weights=[0.3, 0.5, 0.2], k=1)[0]
            available_keys = [key for key in search_space if key in self.wq.default_settings and search_space[key]]
            if not available_keys: return settings_dict
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
            if log_msgs: logger.info(f"[Evolver Mutate] 突变了 {len(log_msgs)} 个设置: {', '.join(log_msgs)}")
            return mutated_settings
        except Exception as e: logger.error(f"[Mutate] 设置突变时发生错误: {e}", exc_info=True); return settings_dict 

    def load_tested_alphas(self):
        with self.tested_alphas_lock:
            if not os.path.exists(self.tested_alphas_logfile): return set()
            try:
                with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content: return set()
                    data = json.loads(content)
                    return set(item.get('expression') for item in data if item.get('expression'))
            except (json.JSONDecodeError, IOError) as e: return set()

    def load_submission_failures(self):
        filepath = self.submission_failure_log_file
        with self.failure_log_lock:
            if not os.path.exists(filepath): self.submission_failures_cache = []; self.submission_failures_set = set(); self.submission_failures_map = {}; return
            try:
                if not os.path.isfile(filepath) or os.path.getsize(filepath) < 2: self.submission_failures_cache = []; self.submission_failures_set = set(); self.submission_failures_map = {}; return
                with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
                if isinstance(data, list):
                    self.submission_failures_cache = data
                    new_set = set(); new_map = {}
                    for item in data:
                        if isinstance(item, dict) and 'expression' in item:
                            expr = item['expression']
                            new_set.add(expr)
                            new_map[expr] = item.get('reason', 'UNKNOWN')
                    if new_set != self.submission_failures_set: self.submission_failures_set = new_set; self.submission_failures_map = new_map
            except Exception as e: logger.error(f"[Failure Log] 加载 {filepath} 时出错: {e}", exc_info=False)

    def load_blacklist_counts(self):
        try:
            with self.blacklist_file_lock: 
                if not os.path.exists(self.invalid_functions_file): return {}
                for _ in range(3):
                    try:
                        with open(self.invalid_functions_file, 'r', encoding='utf-8') as f: content = f.read(); return json.loads(content) if content else {}
                    except json.JSONDecodeError: time.sleep(0.1); continue
                return {} 
        except Exception as e: logger.error(f"加载黑名单文件失败: {e}"); return {}

    def update_blacklist_count(self, identifier_name):
        try:
            with self.blacklist_file_lock:
                current_counts = {}
                if os.path.exists(self.invalid_functions_file):
                    try:
                        with open(self.invalid_functions_file, 'r', encoding='utf-8') as f: content = f.read(); current_counts = json.loads(content) if content else {}
                    except Exception: pass
                current_count = current_counts.get(identifier_name, 0) + 1
                current_counts[identifier_name] = current_count
                self.blacklist_counts = current_counts 
                with open(self.invalid_functions_file, 'w', encoding='utf-8') as f: json.dump(current_counts, f, indent=4)
                if current_count < self.blacklist_max_strikes: logger.warning(f"检测到无效标识符: '{identifier_name}'。计数: {current_count}/{self.blacklist_max_strikes}。")
                else: logger.critical(f"'{identifier_name}' 已达到 {current_count}/{self.blacklist_max_strikes} 次计数，将被永久拉黑。")
        except Exception as e: logger.error(f"保存黑名单计数文件时出错: {e}")

    def is_using_blacklisted_identifier(self, alpha_code: str) -> bool:
        current_counts = self.load_blacklist_counts() 
        if not current_counts: return False
        found_identifiers = self.identifier_pattern.findall(alpha_code)
        if not found_identifiers: return False
        for identifier in found_identifiers:
            if identifier in current_counts and current_counts[identifier] >= self.blacklist_max_strikes:
                logger.warning(f"预检拦截: Alpha '{alpha_code}' 包含了已被拉黑的标识符 '{identifier}'。")
                return True
        return False

    def excavate_one_pearl(self, sample_size=200):
        all_tested = []
        if not os.path.exists(self.tested_alphas_logfile): return None
        try:
            with self.tested_alphas_lock:
                if os.path.getsize(self.tested_alphas_logfile) < 2: return None
                with open(self.tested_alphas_logfile, 'r') as f: all_tested = json.load(f)
        except (IOError, json.JSONDecodeError): return None
        sample_records = random.sample(all_tested, min(len(all_tested), sample_size))
        self.hopeful_alphas_cache = utils.load_hopeful_alphas_safe()
        hopeful_expressions = {alpha.get('expression') for alpha in self.hopeful_alphas_cache}
        potential_pearls = []
        for record in sample_records:
            if not isinstance(record, dict): continue
            expr = record.get('expression')
            if not expr or record.get('status') != 'COMPLETE' or expr in hopeful_expressions or expr in self.submission_failures_set: continue
            passed_count = record.get('passed_checks', 0); fitness = record.get('fitness', -999)
            if passed_count == 3 and fitness > -1.0:
                record['potential_score'] = self._calculate_potential_score(record) 
                potential_pearls.append(record)
        if not potential_pearls: return None
        potential_pearls.sort(key=lambda x: x.get('potential_score', -999), reverse=True)
        best_pearl = potential_pearls[0]
        logger.info(f"考古学家在 {len(sample_records)} 条记录中发现一颗遗珠！潜力分: {best_pearl['potential_score']:.3f}")
        return {"expression": best_pearl['expression'], "performance": best_pearl.get('performance', {})}

    def _get_self_correlation(self, report_or_record) -> float:
        if not isinstance(report_or_record, dict): return 0.0
        perf = report_or_record.get('performance', {})
        if not isinstance(perf, dict): return 0.0
        try:
            checks_list = perf.get('checks', [])
            if isinstance(checks_list, list):
                for check in checks_list:
                    if isinstance(check, dict) and check.get('name') == 'Self-correlation': return float(check.get('value', 0.0))
        except (ValueError, TypeError): pass
        return 0.0

    def _calculate_combined_score(self, report):
        if not isinstance(report, dict): return -float('inf')
        perf = report.get('performance', {})
        if not isinstance(perf, dict): return -float('inf')
        fitness = perf.get('fitness', -999); sharpe = perf.get('sharpe', 0.0); turnover = perf.get('turnover', 1.0)
        checks_summary = report.get('checks_summary', '0 PASS'); passed_count = 0
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
        if self_corr_value > 0.7: self_corr_penalty = (self_corr_value - 0.7) * 5.0
        if report.get('expression') in self.submission_failures_set: self_corr_penalty += 10.0 
        score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
        return score

    def _calculate_potential_score(self, record):
        if not isinstance(record, dict): return -999
        perf = record.get('performance', {})
        if not isinstance(perf, dict): return -999
        try:
            fitness_f = float(record.get('fitness', -999)); passed_count = int(record.get('passed_checks', 0))
            sharpe_f = float(perf.get('sharpe', 0.0)); turnover_f = float(perf.get('turnover', 1.0))
            self_corr_value = self._get_self_correlation(record); self_corr_penalty = 0.0 
            if self_corr_value > 0.7: self_corr_penalty = (self_corr_value - 0.7) * 5.0
            if record.get('expression') in self.submission_failures_set: return -999.0 
            score = fitness_f + (passed_count * 0.2) + (abs(sharpe_f) * 0.3) - (turnover_f * 0.1) - self_corr_penalty
            return score
        except (ValueError, TypeError): return -999

    def load_evolution_seeds(self, total_sample_size=50, wild_card_count=10):
        seeds = []
        try:
            self.hopeful_alphas_cache = utils.load_hopeful_alphas_safe()
            seeds.extend(self.hopeful_alphas_cache)
            logger.info(f"已加载 {len(self.hopeful_alphas_cache)} 个精英策略。")
        except Exception as e: logger.error(f"加载精英池 {utils.HOPEFUL_ALPHAS_FILE} 失败: {e}", exc_info=True); self.hopeful_alphas_cache = []
        pearl = self.excavate_one_pearl()
        if pearl: seeds.append(pearl)
        if not seeds: logger.warning("精英池为空，无法获取进化种子。"); return []
        for seed in seeds: seed['internal_score'] = self._calculate_combined_score(seed)
        seeds.sort(key=lambda x: x['internal_score'], reverse=True) 
        original_seed_count = len(seeds); seeds = [s for s in seeds if s['internal_score'] > -10.0]
        filtered_count = original_seed_count - len(seeds)
        if filtered_count > 0: logger.info(f"[Evolve Seeds] 已从种子池中过滤掉 {filtered_count} 个已知失败的策略。")
        cutoff_index = len(seeds) * 7 // 10
        if cutoff_index == len(seeds) and len(seeds) > 1: cutoff_index = len(seeds) - 1
        top_pool = seeds[:cutoff_index]; bottom_pool = seeds[cutoff_index:]
        logger.info(f"种子池分割: Top {len(top_pool)} (精英), Bottom {len(bottom_pool)} (外卡池)")
        evolution_seeds = []; elite_count = max(0, total_sample_size - wild_card_count); k_elite = 0
        if top_pool: k_elite = min(elite_count, len(top_pool)); evolution_seeds.extend(random.sample(top_pool, k_elite))
        k_wild = 0; actual_wild_card_count = min(wild_card_count, total_sample_size - k_elite)
        if bottom_pool: k_wild = min(actual_wild_card_count, len(bottom_pool)); evolution_seeds.extend(random.sample(bottom_pool, k_wild))
        remaining_needed = total_sample_size - len(evolution_seeds)
        if remaining_needed > 0 and len(seeds) > len(evolution_seeds):
            logger.info(f"种子池较小或抽样后不足，正在补足 {remaining_needed} 个种子...")
            chosen_expressions = {s['expression'] for s in evolution_seeds if 'expression' in s}
            remaining_pool = [s for s in seeds if 'expression' in s and s['expression'] not in chosen_expressions]
            k_remaining = min(remaining_needed, len(remaining_pool))
            if k_remaining > 0: evolution_seeds.extend(random.sample(remaining_pool, k_remaining))
        logger.info(f"策略导师将从 {len(self.hopeful_alphas_cache)} 个精英策略中学习模式。")
        logger.info(f"已抽取 {len(evolution_seeds)} 个种子 (目标: {elite_count} 精英, {wild_card_count} 外卡 => 实际: {k_elite} 精英, {k_wild} 外卡) 作为本轮进化父本。")
        return evolution_seeds

    def analyze_successful_patterns(self, top_k_pool=100, sample_size=7):
        if not self.hopeful_alphas_cache or not isinstance(self.hopeful_alphas_cache, list): return []
        all_expressions = [alpha.get('expression', '') for alpha in self.hopeful_alphas_cache if isinstance(alpha, dict)]
        operator_pattern = re.compile(r'([a-zA-Z_0-9]+)\s*\('); all_operators = []
        for expr in all_expressions:
            if expr and isinstance(expr, str):
                operators_in_expr = operator_pattern.findall(expr); all_operators.extend(operators_in_expr)
        if not all_operators: return []
        most_common_pool = Counter(all_operators).most_common(top_k_pool)
        if not most_common_pool: return []
        operators = [op for op, count in most_common_pool]; weights = [count for op, count in most_common_pool]
        selected_guidance = []; k = min(sample_size, len(operators)); temp_ops = list(operators); temp_weights = list(weights)
        while len(selected_guidance) < k and temp_ops:
            if not temp_weights or sum(temp_weights) <= 0: break
            chosen_op = random.choices(temp_ops, weights=temp_weights, k=1)[0]
            selected_guidance.append(chosen_op)
            try: idx = temp_ops.index(chosen_op); temp_ops.pop(idx); temp_weights.pop(idx)
            except ValueError: break
        logger.info(f"策略导师分析完成: 从 Top {len(operators)} 模式池中，加权随机抽取 {len(selected_guidance)} 个 *唯一* 指导: {selected_guidance}")
        return selected_guidance

    def generate_alpha_idea(self, fields, operators, guidance=None, failed_examples=None):
        result = self.llm.generate_alpha_idea(fields, operators, guidance, failed_examples)
        if result == "BUDGET_EXHAUSTED": logger.warning("[AlphaGenerator] BUDGET_EXHAUSTED (Discover)。"); return "BUDGET_EXHAUSTED"
        if result == "RATE_LIMIT": logger.warning("[AlphaGenerator] RATE_LIMIT (Discover)。"); return "RATE_LIMIT"
        return result

    def generate_evolved_alpha_idea(self, base_alpha_obj, guidance=None):
        if 'performance' not in base_alpha_obj or not base_alpha_obj['performance']: base_alpha_obj['performance'] = {}
        parent_settings = base_alpha_obj['performance'].get('settings', self.wq.default_settings)
        if not parent_settings or not isinstance(parent_settings, dict): parent_settings = self.wq.default_settings
        base_alpha_obj['performance']['settings'] = parent_settings
        parent_expression = base_alpha_obj.get('expression'); special_guidance = None
        if parent_expression in self.submission_failures_set:
            failure_reason = self.submission_failures_map.get(parent_expression, 'UNKNOWN')
            logger.critical(f"[Evolve Guidance] 父本在'失败日志'中 (原因: {failure_reason})！强制“结构性突变”。")
            special_guidance = f"**CRITICAL MUTATION REQUIRED!** Parent is a KNOWN FAILURE (Reason: {failure_reason}). DO NOT micro-optimize. You MUST perform a major structural change (e.g., add new operators, change logic) to escape this failed pattern."
        else:
            parent_self_corr = self._get_self_correlation(base_alpha_obj)
            if parent_self_corr > 0.7:
                logger.warning(f"[Evolve Guidance] 父本 Self-Corr 高 ({parent_self_corr:.3f})，添加特殊指导。")
                special_guidance = f"**PRIORITY: Reduce Self-Correlation!** Parent's simulated correlation is too high ({parent_self_corr:.3f}). Make significant changes to lower it."
        if special_guidance: base_alpha_obj['_special_guidance_high_corr'] = special_guidance
        else: base_alpha_obj.pop('_special_guidance_high_corr', None)
        result = self.llm.generate_evolved_alpha_idea(base_alpha_obj, guidance=guidance) 
        base_alpha_obj.pop('_special_guidance_high_corr', None)
        if result == "BUDGET_EXHAUSTED": logger.warning("[AlphaGenerator] BUDGET_EXHAUSTED (Evolve)。"); return "BUDGET_EXHAUSTED"
        if result == "RATE_LIMIT": logger.warning("[AlphaGenerator] RATE_LIMIT (Evolve)。"); return "RATE_LIMIT"
        if not isinstance(result, dict) or not result.get('expression'): logger.warning("[AlphaGenerator] LLM 未能返回有效的进化 Expression 字典。"); return None 
        mutated_settings = self._mutate_settings(parent_settings)
        evolved_strategy = { "expression": result['expression'], "settings": mutated_settings }
        return evolved_strategy

    def log_tested_alphas(self, reports_to_log):
        with self.tested_alphas_lock:
            all_reports = []
            if os.path.exists(self.tested_alphas_logfile):
                try:
                    if os.path.getsize(self.tested_alphas_logfile) > 1:
                        with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f: content = f.read(); all_reports = json.loads(content) if content else []
                except (IOError, json.JSONDecodeError): all_reports = []
            all_reports.extend(reports_to_log)
            for report in reports_to_log:
                if isinstance(report, dict) and 'expression' in report: self.tested_alphas.add(report['expression'])
            try:
                with open(self.tested_alphas_logfile, 'w', encoding='utf-8') as f: json.dump(all_reports, f, indent=4, ensure_ascii=False)
            except IOError as e: logger.error(f"写入全量日志文件时出错: {e}")

    def archive_purged_alphas(self, purged_reports, reason="淘汰"):
        if not purged_reports: return
        all_archived = []
        if os.path.exists(self.purged_alphas_archive_file):
            try:
                if os.path.getsize(self.purged_alphas_archive_file) > 1:
                    with open(self.purged_alphas_archive_file, 'r', encoding='utf-8') as f: content = f.read(); all_archived = json.loads(content) if content else []
            except (IOError, json.JSONDecodeError): all_archived = []
        for report in purged_reports:
            if isinstance(report, dict): report['archive_reason'] = reason; report['archive_timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        all_archived.extend(purged_reports)
        try:
            with open(self.purged_alphas_archive_file, 'w', encoding='utf-8') as f: json.dump(all_archived, f, indent=4)
            logger.info(f"已将 {len(purged_reports)} 个被淘汰的策略 ({reason}) 存入归档文件。")
        except IOError as e: logger.error(f"写入归档文件时出错: {e}")

    def save_hopeful_reports(self, new_hopeful_reports, max_pool_size=None):
        if max_pool_size is None:
            try: config = utils.load_system_config(); max_pool_size = int(config.get("hopeful_pool_max_size", 20000)) 
            except Exception as e: logger.error(f"[Hopeful Save] 配置读取失败: {e}"); max_pool_size = 20000 
        existing_reports = utils.load_hopeful_alphas_safe()
        combined_reports = existing_reports + new_hopeful_reports
        purged_reports = []; archived_reports = []; unique_reports_map = {}
        for report in combined_reports:
            if isinstance(report, dict) and 'expression' in report: unique_reports_map[report.get('expression')] = report
        logger.info(f"[Hopeful Save] 开始精英池清洗... (共 {len(unique_reports_map)} 个 Unique 策略)")
        expressions_to_archive = set()
        for expr in unique_reports_map:
            if expr in self.submission_failures_set: logger.warning(f"策略 {expr[:40]}... 在失败日志中，拒绝。"); expressions_to_archive.add(expr); continue
            report = unique_reports_map[expr]
            fitness = report.get('performance', {}).get('fitness', -999); checks_summary = report.get('checks_summary', '0 PASS'); passed_count = 0
            try: match = re.search(r'(\d+)\s+PASS', checks_summary or ''); passed_count = int(match.group(1)) if match else 0
            except (ValueError, TypeError): pass
            try: fitness_float = float(fitness)
            except (ValueError, TypeError): fitness_float = -999
            self_corr_value = self._get_self_correlation(report); is_self_corr_ok = self_corr_value < 0.7 
            is_high_quality = fitness_float > 0 and passed_count >= 4; is_high_potential = fitness_float > -0.5 and passed_count >= 5
            if (is_high_quality or is_high_potential) and not is_self_corr_ok: logger.warning(f"策略 {expr[:40]}... Self-Corr 过高 ({self_corr_value:.3f})，拒绝。"); expressions_to_archive.add(expr); continue
            if not (is_high_quality or is_high_potential): expressions_to_archive.add(expr); continue
            purged_reports.append(report)
        for expr in expressions_to_archive:
            if expr in unique_reports_map: archived_reports.append(unique_reports_map[expr])
        logger.info(f"精英池清洗完成: 剩余 {len(purged_reports)} 个 (淘汰 {len(archived_reports)} 个)")
        self.archive_purged_alphas(archived_reports, reason="标准清洗")
        if archived_reports: utils.delete_alphas_safe([r['expression'] for r in archived_reports])
        for report in purged_reports: report['internal_score'] = self._calculate_combined_score(report)
        purged_reports.sort(key=lambda x: x['internal_score'], reverse=True) 
        final_pool = purged_reports[:max_pool_size] 
        if len(purged_reports) > max_pool_size:
            eliminated = purged_reports[max_pool_size:]
            logger.info(f"精英池末位淘汰: {len(final_pool)} (保留前 {max_pool_size})")
            self.archive_purged_alphas(eliminated, reason="末位淘汰")
            utils.delete_alphas_safe([r['expression'] for r in eliminated])
        try:
            if utils.save_hopeful_alphas_safe(final_pool): logger.info(f"已完成精英池更新。当前池中 {len(final_pool)} 个策略。")
            else: logger.error(f"保存精华战报出错。")
        except Exception as e: logger.error(f"保存精华战报发生错误: {e}", exc_info=True)

    def _consumer_worker(self):
        while True:
            strategy = None
            try:
                strategy = self.strategy_queue.get()
                if strategy is None: self.strategy_queue.task_done(); break
                if not isinstance(strategy, dict) or not strategy.get('expression'): self.strategy_queue.task_done(); continue
                idea_expr = strategy['expression']
                logger.info(f"取得策略: {idea_expr[:60]}... (队列: {self.strategy_queue.qsize()})")
                log_report = {"expression": idea_expr, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                result = self.wq.test_alpha(idea_expr, strategy['settings'])
                if result == "RATE_LIMIT": logger.warning(f"遭遇 WQ 429。策略 {idea_expr[:20]}... 被丢弃。"); continue 
                if isinstance(result, dict) and result.get("status") == "ERROR":
                    log_report["status"] = "ERROR"; self.log_tested_alphas([log_report])
                    error_message = str(result.get("message", ""))
                    if "variable" in error_message.lower() or "function" in error_message.lower():
                         match = re.search(r"['\"](\w+)['\"]", error_message)
                         if match: self.update_blacklist_count(match.group(1))
                    logger.warning(f"模拟出错: {idea_expr[:30]}... | {error_message[:100]}...")
                    continue 
                if result in ["TIMEOUT"]:
                    log_report["status"] = result; self.log_tested_alphas([log_report]); logger.warning(f"模拟超时: {idea_expr}"); continue 
                if isinstance(result, dict):
                    is_stats = result.get("is", {}); alpha_id = result.get("id")
                    if not isinstance(is_stats, dict) or not alpha_id: self.strategy_queue.task_done(); continue 
                    checks = is_stats.get("checks", []); passed_count = sum(1 for c in checks if c.get("result") == "PASS")
                    fitness = float(is_stats.get('fitness', -999))
                    log_report["status"] = "COMPLETE"; log_report["fitness"] = fitness; log_report["passed_checks"] = passed_count; log_report["performance"] = is_stats 
                    log_report["performance"]["settings"] = strategy.get("settings", self.wq.default_settings)
                    self.log_tested_alphas([log_report])
                    checks_summary = f"{passed_count} PASS"
                    self_corr_value = self._get_self_correlation(log_report)
                    is_self_corr_ok = self_corr_value < 0.7
                    if idea_expr in self.submission_failures_set: logger.warning(f"策略存在于失败日志，拒绝。"); self.strategy_queue.task_done(); continue
                    is_high_quality = fitness > 0 and passed_count >= 4; is_high_potential = fitness > -0.5 and passed_count >= 5
                    if (is_high_quality or is_high_potential) and is_self_corr_ok:
                        logger.info(f"发现优质策略! Fitness: {fitness:.3f}, Checks: {passed_count}, Self-Corr: {self_corr_value:.3f}")
                        hopeful_report = { "expression": idea_expr, "alpha_id": alpha_id, "grade": result.get("grade", "UNKNOWN"), "timestamp": log_report["timestamp"], "performance": log_report["performance"], "checks_summary": checks_summary }
                        self.save_hopeful_reports([hopeful_report])
                    else:
                         logger.info(f"策略未达标。Fitness: {fitness:.3f}, Checks: {passed_count}, Self-Corr: {self_corr_value:.3f}")
                else: logger.error(f"未知模拟结果: {result}")
            except Exception as exc:
                logger.error(f"Worker error: {exc}", exc_info=True)
            finally: self.strategy_queue.task_done()

    def run(self, mode='discover'):
        logger.info(f"Alpha 生成器启动 | 版本: {CURRENT_GENERATOR_VERSION} | 模式: {mode.upper()} | Workers: {self.concurrency_level}")
        self.fields = self.wq.get_data_fields(); self.operators = self.wq.get_operators()
        if self.operators == "RATE_LIMIT" or not self.fields: logger.error("API 连接失败，请检查网络。"); time.sleep(60)
        logger.info(f"启动 {self.concurrency_level} 个 Worker 线程...")
        for i in range(self.concurrency_level):
            t = threading.Thread(target=self._consumer_worker, name=f"Worker-{i+1}", daemon=True); t.start(); self.consumer_threads.append(t)
        evolution_seeds = []; strategic_guidance = []; failed_examples_for_miner = [] 
        while True:
            try:
                config = utils.load_system_config()
                if self.strategy_queue.qsize() >= self.queue_max_size:
                    if not self._producer_paused_logging_state: logger.info(f"[生产者] 队列满，暂停..."); self._producer_paused_logging_state = True
                    time.sleep(config.get("producer_queue_full_sleep", 10)); continue
                if self._producer_paused_logging_state: logger.info(f"[生产者] 恢复生成..."); self._producer_paused_logging_state = False
                
                gen_interval = config.get("generation_interval_seconds", 0)
                if gen_interval > 0: logger.debug(f"[Economy] 强制休眠 {gen_interval} 秒..."); time.sleep(gen_interval)

                self.load_submission_failures()
                if mode == 'evolve':
                    # [v17.4.0] 动态读取采样参数
                    sample_size = config.get("evolver_sample_size", 50)
                    wild_count = config.get("evolver_wildcard_size", 10)
                    pool_size = config.get("evolver_guidance_pool_size", 100)
                    
                    evolution_seeds = self.load_evolution_seeds(total_sample_size=sample_size, wild_card_count=wild_count)
                    if not evolution_seeds: mode = 'discover'; logger.warning("无进化种子，切换至 Discover 模式。")
                    else: strategic_guidance = self.analyze_successful_patterns(top_k_pool=pool_size)
                
                if mode == 'discover':
                    failed_examples_for_miner = [item['expression'] for item in self.submission_failures_cache if isinstance(item, dict) and 'HIGH_SELF_CORR' in item.get('reason', '').upper()][-20:]
                
                logger.info(f"[生产者] [{mode.upper()}] 生成新 Alpha...")
                idea = None
                if mode == 'discover': idea = self.generate_alpha_idea(self.fields, self.operators, guidance=strategic_guidance, failed_examples=failed_examples_for_miner)
                elif mode == 'evolve':
                    if not evolution_seeds: time.sleep(1); continue
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance) 

                if idea in ["BUDGET_EXHAUSTED", "RATE_LIMIT"]: time.sleep(300); continue 
                if isinstance(idea, dict) and idea.get("expression"):
                    expr = idea.get("expression")
                    if self.is_using_blacklisted_identifier(expr): continue 
                    with self.tested_alphas_lock: 
                         if expr in self.tested_alphas: logger.info("策略重复，跳过。"); continue
                    if expr in self.submission_failures_set: logger.warning("策略在失败库中，跳过。"); continue
                    if is_alpha_syntactically_suspicious(expr): continue
                    try: self.strategy_queue.put(idea); logger.info(f"策略入队。")
                    except queue.Full: pass 
                elif idea is None: logger.warning("生成无效策略。")
            except Exception as e: logger.critical(f"生产者错误: {e}", exc_info=True); time.sleep(60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator v13.3.16 (Dynamic Sampling)') 
    parser.add_argument('--user-id', type=str, required=True); parser.add_argument('--api-key', type=str, required=True)
    parser.add_argument('--batch-size', type=int, default=5); parser.add_argument('--api-config-path', type=str, default="api_config.json")
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve']); parser.add_argument('--log-file', type=str, default='alpha_generator.log')
    args = parser.parse_args()
    utils.setup_logging(args.log_file)
    logger.info(f"正在加载配置...")
    config = utils.load_system_config()
    concurrency = config.get("miner_concurrency" if args.mode == 'discover' else "evolver_concurrency", 1)
    logger.info(f"启动模式: {args.mode}, 并发: {concurrency}")
    wq_client = None
    while wq_client is None:
        try: wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key); logger.info("WQ 登录成功。")
        except Exception as e: logger.error(f"WQ 登录失败: {e}"); time.sleep(30)
    try: AlphaGenerator(wq=wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size, concurrency_level=concurrency).run(mode=args.mode)
    except Exception as e: logger.critical(f"Fatal Error: {e}", exc_info=True)
