# alpha_generator_ollama.py (已修正)
import argparse
import logging
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from openai import OpenAI
from datetime import datetime
import shutil

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

# --- WorldQuant API 部分 ---
class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = requests.Session()
        self._authenticate()

    def _authenticate(self):
        url = f"{self.base_url}/authentication"
        try:
            response = self.session.post(url, auth=(self.user_id, self.api_key))
            response.raise_for_status()
            logger.info("WorldQuant Brain authentication successful.")
        except requests.exceptions.RequestException as e:
            logger.error(f"WorldQuant Brain authentication failed: {e}")
            raise

    def get_data_fields(self):
        logger.info("正在使用硬编码的、绝对安全的官方核心数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个核心数据字段。")
        return safe_fields

    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if 'results' in data and isinstance(data['results'], list):
                operators = [str(op) for op in data['results']]
            else:
                operators = [str(op) for op in data]
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
        
        progress_url = None
        for attempt in range(2):
            try:
                submit_response = self.session.post(submit_url, json=payload, timeout=120)
                if submit_response.status_code == 401:
                    logger.warning("第1步：提交时认证失败 (401)，正在尝试重新认证...")
                    self._authenticate()
                    if attempt == 0: continue
                
                submit_response.raise_for_status()
                progress_url = submit_response.headers.get('location')
                
                if not progress_url:
                    logger.error(f"提交模拟任务后，未能从Header获取 location。")
                    return None
                
                logger.info(f"成功提交模拟任务，进度URL: {progress_url}")
                break
            except requests.exceptions.RequestException as e:
                error_content = "No response body"
                if e.response is not None:
                    try: error_content = e.response.json()
                    except json.JSONDecodeError: error_content = e.response.text
                logger.error(f"第1步：提交模拟任务失败 '{alpha_expression}': {e} - Response: {error_content}")
                return None
        else:
            logger.error("重新认证后，提交模拟任务依然失败。")
            return None

        polling_start_time = time.time()
        while time.time() - polling_start_time < 600:
            try:
                poll_response = self.session.get(progress_url, timeout=120)
                if poll_response.status_code == 401:
                    logger.warning("第2/3步：轮询时认证失败 (401)，正在尝试重新认证...")
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
                    final_response = self.session.get(final_alpha_url)
                    final_data = final_response.json()
                    logger.info(f"Alpha '{alpha_id}' 模拟完成。")
                    return final_data
                elif status == "ERROR":
                    logger.error(f"Alpha 模拟出错，服务器返回的完整错误报告: {result_data}")
                    return None
                else:
                    logger.debug(f"Alpha 仍在模拟中... 状态: {status}")
                    time.sleep(5)
            except requests.exceptions.RequestException as e:
                logger.error(f"第2/3步：轮询结果失败: {e}")
                time.sleep(10)
            except Exception as e:
                logger.error(f"处理轮询结果时发生未知错误: {e}")
                return None
        logger.warning(f"Alpha 模拟超时（超过10分钟）。")
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
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                    return set(item.get('expression') for item in data if item.get('expression'))
                return set()
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.warning(f"加载 {self.hopeful_alphas_file} 出错或格式不兼容: {e}, 将创建一个新的记录文件。")
            return set()

    def generate_alpha_idea(self, fields, operators):
        field_list = ", ".join(fields)
        core_operators = [
            'rank', 'ts_corr', 'ts_delta', 'ts_decay_linear', 'ts_mean', 'ts_std_dev', 
            'ts_zscore', 'multiply', 'subtract', 'divide', 'add', 'log', 'signed_power'
        ]
        operator_list = ", ".join(core_operators)
        prompt = f"""
        You are a world-class Quantitative Analyst creating alphas for WorldQuant. Your goal is to generate a single, novel, and syntactically correct alpha expression.
        Follow these rules strictly:
        1.  **Use ONLY the provided fields and operators.** Do not invent new ones.
        2.  **The expression MUST end with a semicolon (;).**
        3.  **Structure:** Combine multiple operators and fields. Simple expressions like `close;` or `rank(close);` are not useful.
        4.  **Logic:** The alpha should represent a plausible financial logic (e.g., momentum, mean-reversion, value).
        5.  **Output Format:** Your entire response MUST be ONLY the raw alpha expression. Do NOT include any explanations, markdown like \`\`\`alpha\`\`\`, or any other text.
        **Available Data Fields:** {field_list}
        **Core Allowed Operators:** {operator_list}
        **Example of a valid, complex expression:**
        `rank(ts_corr(vwap, ts_mean(volume, 20), 5)) - rank(ts_delta(close, 7));`
        New Alpha Expression:
        """
        try:
            chat_completion = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.9,
            )
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
            if not idea.endswith(';'): idea += ';'
            return idea
        except Exception as e:
            logger.error(f"从 API 生成 Alpha 失败: {e}")
            return None

    def test_alpha(self, alpha):
        clean_alpha = alpha.strip()
        if not clean_alpha: return None
        if clean_alpha in self.tested_alphas:
            logger.info(f"跳过已测试的 Alpha: {clean_alpha}")
            return None
        logger.info(f"正在测试新 Alpha: {clean_alpha}")
        return self.wq.test_alpha(clean_alpha)
            
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
            self.tested_alphas.add(report['expression'])

        try:
            existing_reports.sort(key=lambda x: x.get('performance', {}).get('fitness', 0), reverse=True)
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
            time.sleep(60)
            return

        while True:
            logger.info(f"开始新一轮 Alpha 生成，目标数量: {self.batch_size}")
            alpha_ideas = [self.generate_alpha_idea(fields, operators) for _ in range(self.batch_size)]
            alpha_ideas = [idea for idea in alpha_ideas if idea]
            
            logger.info(f"成功生成 {len(alpha_ideas)} 个新 Alpha 表达式。")
            if not alpha_ideas:
                logger.info("本轮未生成有效 Alpha。")
            else:
                new_reports = []
                logger.info("开始串行测试新生成的 Alpha (一次一个)...")
                for idea in alpha_ideas:
                    result = self.test_alpha(idea)
                    if result:
                        try:
                            is_stats = result.get("is", {})
                            if not is_stats: continue
                            alpha_id = result.get("id")
                            if not alpha_id: continue

                            # --- [核心修正 2] ---
                            # 构建 Alpha 详情页链接并添加到报告中
                            result_url = f"https://platform.worldquantbrain.com/alphas/regular/{alpha_id}"

                            report = {
                                "expression": result.get("regular", {}).get("code"),
                                "alpha_id": alpha_id,
                                "result_url": result_url, # <--- 新增字段
                                "grade": result.get("grade", "UNKNOWN"),
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "performance": {
                                    "sharpe": is_stats.get("sharpe"), "fitness": is_stats.get("fitness"),
                                    "turnover": is_stats.get("turnover"),
                                },
                            }
                            new_reports.append(report)
                            logger.info(f"生成新的Alpha战报: {report['expression']} - Fitness: {report['performance']['fitness']}")
                        except Exception as e:
                            logger.error(f"生成战报时出错: {e}")

                if new_reports:
                    self.save_and_update_reports(new_reports)
            
            sleep_time = 600
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