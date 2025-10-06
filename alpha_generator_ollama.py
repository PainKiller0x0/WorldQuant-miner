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

# --- 日志配置 ---
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(os.path.join(LOG_DIR, "alpha_generator.log")),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

def is_alpha_syntactically_suspicious(alpha_code: str) -> bool:
    ts_functions_pattern = r'ts_([a-zA-Z_]+)\(([^,)]+)\)'
    match = re.search(ts_functions_pattern, alpha_code)
    if match:
        logger.warning(f"本地预检失败: Alpha '{alpha_code}' 中的函数 '{match.group(0)}' 可能缺少 lookback 参数。已拒绝。")
        return True
    return False

class WorldQuant:
    # ... [這部分代碼與上一版完全相同，為了简洁此處省略] ...
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
            op_list = []
            if isinstance(data, dict):
                op_list = data.get('results', [])
            elif isinstance(data, list):
                op_list = data
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
        
        payload = {
            'type': 'REGULAR', 'regular': alpha_expression,
            'settings': current_settings
        }
        
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

        POLLING_TIMEOUT = 900
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
    # ... [__init__ 等方法与上一版类似] ...
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
        self.tested_alphas = self.load_tested_alphas()
        self.hopeful_alphas_cache = []

    def load_tested_alphas(self):
        if not os.path.exists(self.tested_alphas_logfile):
            return set()
        try:
            with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return set()
                data = json.loads(content)
                return set(item.get('expression') for item in data if item.get('expression'))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"加载 {self.tested_alphas_logfile} 出错: {e}, 将创建一个新的记录文件。")
            return set()
    
    def load_hopeful_alphas_for_evolution(self):
        if not os.path.exists(self.hopeful_alphas_file):
            logger.warning("进化模式：找不到 hopeful_alphas.json 文件，将退化为发现模式。")
            return []
        try:
            with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return []
                self.hopeful_alphas_cache = json.loads(content)
                return self.hopeful_alphas_cache
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载 hopeful_alphas.json 用于进化时出错: {e}")
            return []

    def generate_alpha_idea(self, fields, operators):
        # ... [代码不变] ...
        field_list = ", ".join(fields)
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power']
        operator_list = ", ".join(core_operators)
        prompt = f"""
        You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.
        Follow these rules strictly:
        1.  **Use ONLY the provided fields and operators.**
        2.  **The expression MUST end with a semicolon (;).**
        3.  **IMPORTANT SYNTAX:** All functions starting with `ts_` (like `ts_corr`, `ts_mean`, etc.) MUST have a second integer argument for the lookback period (e.g., `ts_mean(close, 10)`).
        4.  **Structure:** Combine multiple operators and fields.
        5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression.
        **Available Data Fields:** {field_list}
        **Core Allowed Operators:** {operator_list}
        New Alpha Expression:
        """
        try:
            chat_completion = self.client.chat.completions.create(model=self.model_name, messages=[{"role": "user", "content": prompt}], max_tokens=100, temperature=0.9)
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
            if idea and not idea.endswith(';'): idea += ';'
            return {"expression": idea, "settings": {}}
        except Exception as e:
            logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None

    def generate_evolved_alpha_idea(self, base_alpha_obj, fields, operators):
        # --- [核心修改] 使用更兼容的Prompt和后处理逻辑 ---
        base_expression = base_alpha_obj.get('expression')
        base_settings = base_alpha_obj.get('performance', {}).get('settings', self.wq.default_settings)

        prompt = f"""
        You are a world-class Quantitative Analyst evolving alpha STRATEGIES (expression + settings) for WorldQuant.
        Your goal is to take a proven, successful alpha strategy and create a new, improved variation.

        **Base Successful Strategy:**
        - **Expression:** `{base_expression}`
        - **Current Settings:** `{json.dumps(base_settings)}`

        **Your Task:** Create a new strategy by applying ONE of the following evolution strategies:
        1.  **Evolve Expression:** Make a small, creative change to the expression.
        2.  **Evolve Settings:** Make a small, logical change to ONE of the tunable numeric settings (`delay`, `decay`, `truncation`).

        **Strict Rules:**
        - Your response MUST be a valid JSON object wrapped in a markdown code block.
        - The JSON MUST contain two keys: "expression" (string) and "settings" (a dictionary object).
        - If evolving expression, "settings" should be empty (`{{}}`).
        - If evolving settings, "expression" MUST be identical to the base expression.
        - The new strategy MUST be different from the base strategy.

        **Example Response (Evolving Settings):**
        ```json
        {{
          "expression": "{base_expression}",
          "settings": {{
            "decay": 5
          }}
        }}
        ```
        New Evolved Strategy (JSON in a markdown block):
        """
        try:
            chat_completion = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.7,
            )
            response_text = chat_completion.choices[0].message.content.strip()
            
            # 使用正则表达式从Markdown代码块中提取JSON
            json_match = re.search(r'```json\s*([\s\S]+?)\s*```', response_text)
            if not json_match:
                # 如果找不到代码块，尝试直接解析整个回复
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
            
    # ... [log_tested_alphas 和 save_hopeful_reports 函数不变] ...
    def log_tested_alphas(self, reports_to_log):
        all_reports = []
        if os.path.exists(self.tested_alphas_logfile):
            try:
                with open(self.tested_alphas_logfile, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: all_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {self.tested_alphas_logfile}，将创建新的日志文件。")

        for report in reports_to_log:
            all_reports.append(report)
            if 'expression' in report:
                self.tested_alphas.add(report['expression'])
        
        try:
            with open(self.tested_alphas_logfile, 'w', encoding='utf-8') as f:
                json.dump(all_reports, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"写入全量日志文件时出错: {e}")

    def save_hopeful_reports(self, new_hopeful_reports):
        existing_reports = []
        if os.path.exists(self.hopeful_alphas_file):
            try:
                with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: existing_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {self.hopeful_alphas_file}，将创建新的精华文件。")
        
        existing_reports.extend(new_hopeful_reports)

        try:
            existing_reports.sort(key=lambda x: x.get('performance', {}).get('fitness', -999), reverse=True)
            with open(self.hopeful_alphas_file, 'w', encoding='utf-8') as f:
                json.dump(existing_reports, f, indent=4, ensure_ascii=False)
            logger.info(f"已将 {len(new_hopeful_reports)} 份新的高质量战报更新到 {self.hopeful_alphas_file}，并按Fitness排序。")
        except IOError as e:
            logger.error(f"保存精华战报文件时出错: {e}")

    def run(self, mode='discover', concurrency_level=2, sleep_time=10):
        # ... [run方法与上一版完全相同] ...
        logger.info(f"Alpha 生成器启动 | 模式: {mode.upper()} | 并发等级: {concurrency_level} | 轮间间隔: {sleep_time}s")
        fields = self.wq.get_data_fields()
        operators = self.wq.get_operators()
        if not fields or not operators:
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            time.sleep(60); return
        
        evolution_seeds = []
        if mode == 'evolve':
            evolution_seeds = self.load_hopeful_alphas_for_evolution()
            if not evolution_seeds:
                mode = 'discover'
                logger.warning("进化模式无法启动（无可用种子），已自动切换到发现模式。")

        while True:
            logger.info(f"[{mode.upper()}] 开始新一轮 Alpha 生成，目标数量: {self.batch_size}")
            
            strategies_to_test = []
            if mode == 'discover':
                for _ in range(self.batch_size):
                    idea = self.generate_alpha_idea(fields, operators)
                    if idea: strategies_to_test.append(idea)
            elif mode == 'evolve':
                for _ in range(self.batch_size):
                    base_alpha_obj = random.choice(evolution_seeds)
                    idea = self.generate_evolved_alpha_idea(base_alpha_obj, fields, operators)
                    if idea: strategies_to_test.append(idea)

            valid_strategies = [
                s for s in strategies_to_test 
                if s and s.get("expression") and s.get("expression") not in self.tested_alphas
                and not is_alpha_syntactically_suspicious(s.get("expression"))
            ]
            
            logger.info(f"成功生成 {len(valid_strategies)} 个通过预检且待测试的新策略。")
            
            if valid_strategies:
                new_hopeful_reports = []
                reports_to_log = []
                logger.info(f"开始并行测试 {len(valid_strategies)} 个新策略，并发数: {concurrency_level}...")
                
                with ThreadPoolExecutor(max_workers=concurrency_level) as executor:
                    future_to_strategy = {
                        executor.submit(self.wq.test_alpha, s['expression'], s['settings']): s 
                        for s in valid_strategies
                    }
                    
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
                                reports_to_log.append(log_report)

                                if fitness > 0 and passed_count >= 4:
                                    logger.info(f"发现一个高质量策略！ Fitness: {fitness:.3f}, Checks: {passed_count} PASS. Alpha: {idea_expr}")
                                    
                                    failed_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "FAIL")
                                    pending_count = sum(1 for check in checks if isinstance(check, dict) and check.get("result") == "PENDING")
                                    check_details = [check.get("details", f"{check.get('name')}: {check.get('result')}") for check in checks if isinstance(check, dict)]
                                    checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                                    hopeful_report = {
                                        "expression": result.get("regular", {}).get("code"), "alpha_id": alpha_id,
                                        "result_url": f"https://platform.worldquantbrain.com/alphas/regular/{alpha_id}",
                                        "grade": result.get("grade", "UNKNOWN"), "timestamp": log_report["timestamp"],
                                        "performance": is_stats, "checks_summary": checks_summary, "checks_details": check_details,
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

            logger.info(f"本轮结束。等待{sleep_time}秒开始下一轮...")
            time.sleep(sleep_time)


if __name__ == "__main__":
    # ... [命令行参数部分与上一版完全相同] ...
    parser = argparse.ArgumentParser(description='Alpha Generator using a generic API endpoint')
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    parser.add_argument('--concurrency', type=int, default=2, help="Number of alphas to test concurrently")
    parser.add_argument('--sleep', type=int, default=10, help="Seconds to wait between generation cycles")
    parser.add_argument('--mode', type=str, default='discover', choices=['discover', 'evolve'], help="Generation mode: 'discover' from scratch or 'evolve' from hopeful alphas.")
    args = parser.parse_args()

    try:
        wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
        generator = AlphaGenerator(wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size)
        generator.run(mode=args.mode, concurrency_level=args.concurrency, sleep_time=args.sleep)
    except Exception as e:
        logger.critical(f"启动 Alpha 生成器时发生致命错误: {e}", exc_info=True)