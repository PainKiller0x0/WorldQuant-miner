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

# --- 日志配置 ---
def setup_logging(log_file):
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(log_dir, log_file)),
                            logging.StreamHandler()
                        ])
logger = logging.getLogger(__name__)

def is_alpha_syntactically_suspicious(alpha_code: str) -> bool:
    ts_functions_pattern = r'ts_([a-zA-Z_]+)\(([^,)]+)\)'
    match = re.search(ts_functions_pattern, alpha_code)
    if match:
        params = match.group(2).split(',')
        if len(params) == 1 and not params[0].strip().isdigit():
            logger.warning(f"本地预检失败: Alpha '{alpha_code}' 中的函数 '{match.group(0)}' 可能缺少 lookback 参数。已拒绝。")
            return True
    return False

class WorldQuant:
    # ... (这部分代码与上一版完全相同，为节省篇幅已省略) ...
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
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                raise

    def get_data_fields(self):
        logger.info("正在使用硬编码的、绝对安全的官方核心数据字段列表...")
        safe_fields = ["open", "high", "low", "close", "volume", "vwap"]
        logger.info(f"成功加载 {len(safe_fields)} 个核心数据字段。")
        return safe_fields

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
                    return "ERROR"
                else:
                    logger.debug(f"Alpha '{alpha_expression}' 仍在模拟中... 状态: {status}")
                    time.sleep(10)
            except requests.exceptions.RequestException as e:
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

    # --- v7.0: 考古学家内置函数 ---
    def excavate_one_pearl(self, sample_size=200):
        if not os.path.exists(self.tested_alphas_logfile):
            return None

        try:
            with open(self.tested_alphas_logfile, 'r') as f:
                all_tested = json.load(f)
        except (IOError, json.JSONDecodeError):
            logger.error(f"考古挖掘失败：无法读取 {self.tested_alphas_logfile}")
            return None

        # 随机抽样，避免每次都读取整个大文件
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
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']
        operator_list = ", ".join(core_operators)
        
        prompt_lines = [
            "You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.",
            "Follow these rules strictly:",
            "1.  **Use ONLY the provided fields and operators.**",
            "2.  **The expression MUST end with a semicolon (;).**",
            "3.  **IMPORTANT SYNTAX:** All functions starting with `ts_` (like `ts_corr`, `ts_mean`, etc.) MUST have a second integer argument for the lookback period (e.g., `ts_mean(close, 10)`).",
            "4.  **Complexity:** To ensure fast simulations, try to keep the total number of operators below 10.",
            "5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression."
        ]
        
        if guidance:
            prompt_lines.append(f"**Strategic Guidance:** Our analysis shows that expressions using `{', '.join(guidance)}` tend to be more successful. Try to incorporate these patterns.")

        prompt_lines.extend([
            f"**Available Data Fields:** {field_list}",
            f"**Core Allowed Operators:** {operator_list}",
            "New Alpha Expression:"
        ])
        prompt = "\n".join(prompt_lines)

        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=100, temperature=0.9)
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
            if idea and not idea.endswith(';'): idea += ';'
            return {"expression": idea, "settings": {}}
        except Exception as e:
            logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None

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
            "1.  **Evolve Expression:** Make a small, creative change to the expression. Prioritize using the strategic guidance if available. Keep the expression concise (under 10 operators if possible).",
            "2.  **Evolve Settings:** Make a small, logical change to ONE numeric setting (`delay`, `decay`, `truncation`).",
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
            logger.error(f"从 API '进化' Alpha 策略失败: {e}")
            return None

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
        fields = self.wq.get_data_fields()
        operators = self.wq.get_operators()
        if not fields or not operators:
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            time.sleep(60); return
        
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
            current_batch_size = 1 if is_first_run else self.batch_size
            current_concurrency = 1 if is_first_run else concurrency_level

            if is_first_run:
                logger.info("***** 首次运行，进入安全模式 (batch=1, concurrency=1) *****")

            logger.info(f"[{mode.upper()}] 开始新一轮 Alpha 生成，目标数量: {current_batch_size}")
            
            strategies_to_test = []
            if mode == 'discover':
                for _ in range(current_batch_size):
                    idea = self.generate_alpha_idea(fields, operators, guidance=strategic_guidance)
                    if idea: strategies_to_test.append(idea)
            elif mode == 'evolve':
                for _ in range(current_batch_size):
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, guidance=strategic_guidance)
                    if idea: strategies_to_test.append(idea)

            valid_strategies = [s for s in strategies_to_test if s and s.get("expression") and s.get("expression") not in self.tested_alphas and not is_alpha_syntactically_suspicious(s.get("expression"))]
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
                            if result in ["TIMEOUT", "ERROR"]:
                                log_report["status"] = result
                                reports_to_log.append(log_report)
                                logger.warning(f"Alpha 模拟{result}，已记录并丢弃: {idea_expr}")
                                continue
                            
                            if result:
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

            # 无论是否有新策略，进化者都需要重新加载种子池
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