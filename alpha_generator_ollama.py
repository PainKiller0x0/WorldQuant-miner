import argparse
import logging
import json
import os
import time
import requests
from requests.adapters import HTTPAdapter, Retry
from openai import OpenAI
from datetime import datetime
import threading
import re

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
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        self.auth_lock = threading.Lock()
        self._authenticate()

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

    def test_alpha(self, alpha_expression: str):
        submit_url = f"{self.base_url}/simulations"
        payload = {
            'type': 'REGULAR', 'regular': alpha_expression,
            'settings': {
                'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA',
                'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY',
                'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
                'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False,
            }
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
                    return None
                else:
                    logger.debug(f"Alpha 仍在模拟中... 状态: {status}")
                    time.sleep(10)
            except requests.exceptions.RequestException as e:
                logger.error(f"轮询结果失败: {e}，将在15秒后重试...")
                time.sleep(15)
            except Exception as e:
                logger.error(f"处理轮询结果时发生未知错误: {e}")
                return None
        
        logger.warning(f"Alpha 模拟超时（超过 {POLLING_TIMEOUT/60:.0f} 分钟）。")
        return None

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
        self.tested_alphas = self.load_tested_alphas()

    def load_tested_alphas(self):
        if not os.path.exists(self.hopeful_alphas_file):
            return set()
        try:
            with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return set()
                data = json.loads(content)
                return set(item.get('expression') for item in data if item.get('expression'))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"加载 {self.hopeful_alphas_file} 出错: {e}, 将创建一个新的记录文件。")
            return set()

    def generate_alpha_idea(self, fields, operators):
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
            return idea
        except Exception as e:
            logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None

    def save_and_update_reports(self, new_reports):
        existing_reports = []
        if os.path.exists(self.hopeful_alphas_file):
            try:
                with open(self.hopeful_alphas_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: existing_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {self.hopeful_alphas_file}，将创建新的战报。")
        
        for report in new_reports:
            existing_reports.append(report)
            if 'expression' in report:
                self.tested_alphas.add(report['expression'])

        try:
            existing_reports.sort(key=lambda x: x.get('performance', {}).get('fitness', -999), reverse=True)
            with open(self.hopeful_alphas_file, 'w', encoding='utf-8') as f:
                json.dump(existing_reports, f, indent=4, ensure_ascii=False)
            logger.info(f"已将 {len(new_reports)} 份新战报更新到 {self.hopeful_alphas_file}，并按Fitness排序。")
        except IOError as e:
            logger.error(f"保存战报文件时出错: {e}")

    def run(self):
        logger.info("Alpha 生成器启动 (自建 API 模式)...")
        fields = self.wq.get_data_fields()
        operators = self.wq.get_operators()
        if not fields or not operators:
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            time.sleep(60); return

        while True:
            logger.info(f"开始新一轮 Alpha 生成，目标数量: {self.batch_size}")
            alpha_ideas = [self.generate_alpha_idea(fields, operators) for _ in range(self.batch_size)]
            
            valid_ideas_to_test = []
            for idea in alpha_ideas:
                if idea and idea not in self.tested_alphas:
                    if not is_alpha_syntactically_suspicious(idea):
                        valid_ideas_to_test.append(idea)
                    else:
                        self.tested_alphas.add(idea) 
            
            logger.info(f"成功生成 {len(valid_ideas_to_test)} 个通过预检且待测试的新 Alpha 表达式。")
            
            if valid_ideas_to_test:
                new_reports = []
                logger.info("开始串行测试新生成的 Alpha (一次一个)...")
                for idea in valid_ideas_to_test:
                    logger.info(f"正在测试新 Alpha: {idea}")
                    result = self.wq.test_alpha(idea)
                    
                    if result:
                        try:
                            is_stats = result.get("is", {})
                            alpha_id = result.get("id")
                            if not is_stats or not alpha_id: continue

                            # --- [核心修正] ---
                            # 1. 解析详细的检查结果
                            checks = is_stats.get("checks", [])
                            passed_count, failed_count, pending_count = 0, 0, 0
                            check_details = []
                            if isinstance(checks, list):
                                for check in checks:
                                    res = check.get("result", "UNKNOWN")
                                    if res == "PASS": passed_count += 1
                                    elif res == "FAIL": failed_count += 1
                                    elif res == "PENDING": pending_count += 1
                                    # 保存详细的描述信息
                                    check_details.append(check.get("details", f"{check.get('name')}: {res}"))
                            
                            checks_summary = f"{passed_count} PASS / {failed_count} FAIL / {pending_count} PENDING"

                            # 2. 将检查结果存入报告
                            report = {
                                "expression": result.get("regular", {}).get("code"),
                                "alpha_id": alpha_id,
                                "result_url": f"https://platform.worldquantbrain.com/alphas/regular/{alpha_id}",
                                "grade": result.get("grade", "UNKNOWN"),
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "performance": is_stats,
                                "checks_summary": checks_summary,
                                "checks_details": check_details,
                            }
                            
                            # 3. 生成更丰富的日志
                            perf_items = is_stats.items()
                            stats_str = ", ".join([f"{key}: {value:.3f}" for key, value in perf_items if isinstance(value, (int, float))])
                            logger.info(f"生成新的Alpha战报 [{checks_summary}] -> {stats_str}")
                            
                            new_reports.append(report)
                        except Exception as e:
                            logger.error(f"处理已完成的 Alpha 结果时出错: {e}")
                
                if new_reports:
                    self.save_and_update_reports(new_reports)
            
            sleep_time = 300
            logger.info(f"本轮结束。等待{sleep_time}秒（{sleep_time/60:.1f}分钟）开始下一轮...")
            time.sleep(sleep_time)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator using a generic API endpoint')
    parser.add_argument('--user-id', type=str, required=True, help="WorldQuant User ID (email)")
    parser.add_argument('--api-key', type=str, required=True, help="WorldQuant API Key (password)")
    parser.add_argument('--batch-size', type=int, default=5, help="Number of alphas to generate per cycle")
    parser.add_argument('--api-config-path', type=str, default="api_config.json", help="Path to the API configuration file")
    args = parser.parse_args()

    try:
        wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
        generator = AlphaGenerator(wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size)
        generator.run()
    except Exception as e:
        logger.critical(f"启动 Alpha 生成器时发生致命错误: {e}", exc_info=True)