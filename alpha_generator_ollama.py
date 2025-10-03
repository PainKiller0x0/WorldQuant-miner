# === 终局之战版，替换 alpha_generator_ollama.py 的所有内容 (适配Google原生API) ===
import argparse
import logging
import json
import os
import time
import requests
from datetime import datetime
import shutil
import threading

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
            response = self.session.post(url, auth=(self.user_id, self.api_key), timeout=30)
            response.raise_for_status()
            logger.info("WorldQuant Brain authentication successful.")
        except requests.exceptions.RequestException as e:
            logger.error(f"WorldQuant Brain authentication failed: {e}")
            raise

    def get_data_fields(self):
        logger.info("正在使用优化版的、包含核心财务数据的数据字段列表")
        enhanced_fields = ["open", "high", "low", "close", "volume", "vwap", "turnover", "market_cap", "revenue", "assets", "cashflow_op"]
        logger.info(f"成功加載 {len(enhanced_fields)} 個优选数据字段。")
        return enhanced_fields

    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            operators = [str(item) for item in data if item]
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.error(f"Failed to get operators: {e}")
            return []

    def test_alpha(self, alpha_expression: str):
        submit_url = f"{self.base_url}/simulations"
        payload = {'type': 'REGULAR', 'regular': alpha_expression, 'settings': {'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA', 'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY', 'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY', 'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False}}
        progress_url = None
        for attempt in range(2):
            try:
                submit_response = self.session.post(submit_url, json=payload, timeout=120)
                if submit_response.status_code == 401:
                    logger.warning("提交时认证失败 (401)，正在尝试重新认证...")
                    self._authenticate()
                    continue
                submit_response.raise_for_status()
                progress_url = submit_response.headers.get('location')
                if not progress_url:
                    logger.error(f"未能从Header获取 location。")
                    return None
                logger.info(f"成功提交模拟任务，进度URL: {progress_url}")
                break
            except requests.exceptions.RequestException as e:
                logger.error(f"提交模拟任务失败: {e}")
                return None
        else:
            logger.error("重新认证后，提交模拟任务依然失败。")
            return None
        
        polling_start_time = time.time()
        while time.time() - polling_start_time < 600:
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
                        logger.error("模拟完成但未找到alpha_id")
                        return None
                    final_alpha_url = f"{self.base_url}/alphas/{alpha_id}"
                    final_response = self.session.get(final_alpha_url)
                    return final_response.json()
                elif status == "ERROR":
                    logger.error(f"Alpha 模拟出错: {result_data}")
                    return None
                time.sleep(5)
            except requests.exceptions.RequestException as e:
                logger.error(f"轮询结果失败: {e}")
                time.sleep(10)
        logger.warning(f"Alpha 模拟超时。")
        return None

# --- AlphaGenerator ---
class AlphaGenerator:
    def __init__(self, wq, api_config_path, batch_size=5):
        self.wq = wq
        self.batch_size = batch_size
        try:
            with open(api_config_path, 'r') as f: config = json.load(f)
            self.api_base_url = config['base_url'].rstrip('/')
            self.api_keys = config.get('api_keys', [])
            if not self.api_keys: raise ValueError("api_keys 列表不能为空")
            self.current_key_index = 0
            self.key_lock = threading.Lock()
            logger.info(f"API client initialized for Google Native format at base URL: {self.api_base_url} with {len(self.api_keys)} keys.")
        except Exception as e: logger.critical(f"加载 API 配置失败: {e}"); raise
        self.tested_alphas_file = "tested_alphas.json"
        self.tested_alphas = self.load_tested_alphas()

    def get_next_key(self):
        with self.key_lock:
            key = self.api_keys[self.current_key_index]
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            logger.info(f"正在使用 Key (索引 {self.current_key_index}): {key[:8]}...")
            return key

    def load_tested_alphas(self):
        if not os.path.exists(self.tested_alphas_file): return set()
        try:
            with open(self.tested_alphas_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return set()
                data = json.loads(content)
                if isinstance(data, list) and data:
                    if isinstance(data[0], dict): return set(item.get('expression') for item in data if item.get('expression'))
                    if isinstance(data[0], str): return set(data)
            return set()
        except Exception as e:
            logger.warning(f"加载 tested_alphas.json 出错: {e}, 将创建新文件。")
            return set()

    def generate_alpha_idea(self, fields, operators):
        field_list = ", ".join(fields)
        core_operators = ['rank', 'ts_corr', 'ts_delta', 'ts_mean', 'ts_std_dev', 'ts_zscore', 'subtract', 'divide', 'add']
        operator_list = ", ".join(core_operators)
        
        prompt = f"""Generate a single, unique FASTEXPR alpha expression.
Available Data Fields: {field_list}
Available Operators: {operator_list}
Instructions:
1. Output ONLY the alpha expression.
2. The expression must end with a semicolon ';'.
3. Combine several operators to create a non-trivial expression.
Example: ts_rank(rank(ts_corr(low, volume, 10)), 5);
New Alpha Expression:"""
        
        api_key_to_use = self.get_next_key()
        
        # <--- [核心修改] 适配Google原生API格式 ---
        model_name = "gemini-2.5-flash" # 使用官方推荐的稳定模型名
        url = f"{self.api_base_url}/v1beta/models/{model_name}:generateContent?key={api_key_to_use}"
        
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.95,
                "maxOutputTokens": 150
            }
        }
        
        try:
            logger.info(f"正在向Google原生API格式发送请求: {url}")
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            
            response.raise_for_status()
            completion = response.json()
            
            # Google原生API的响应解析路径
            raw_idea = completion['candidates'][0]['content']['parts'][0]['text']
            
            logger.info(f"AI Model Raw Output: '{raw_idea}'")
            idea = raw_idea.strip().replace('`', '')
            if idea and not idea.endswith(';'): idea += ';'
            return idea
        except requests.exceptions.RequestException as e:
            logger.error(f"从 API 生成 Alpha 失败: {e}")
            if e.response: 
                try:
                    logger.error(f"API 响应: {e.response.json()}")
                except json.JSONDecodeError:
                    logger.error(f"API 响应 (非JSON): {e.response.text}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"解析 API 响应时出错: {e}, 原始响应: {completion}")
            return None

    def test_alpha(self, alpha):
        clean_alpha = alpha.strip()
        if not clean_alpha or clean_alpha in self.tested_alphas: return None
        logger.info(f"正在测试新 Alpha: {clean_alpha}")
        return self.wq.test_alpha(alpha)
            
    def save_and_update_reports(self, new_reports):
        file_path = 'hopeful_alphas.json'
        existing_reports = []
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if content: existing_reports = json.loads(content)
            except (IOError, json.JSONDecodeError):
                logger.warning(f"无法解析 {file_path}，将创建新的战报。")
        
        for report in new_reports:
            existing_reports.append(report)
            if 'expression' in report and report['expression']:
                self.tested_alphas.add(report['expression'])

        try:
            existing_reports.sort(key=lambda x: x.get('performance', {}).get('fitness', 0), reverse=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_reports, f, indent=4, ensure_ascii=False)
            logger.info(f"已将 {len(new_reports)} 份新战报更新到 {file_path}，并按Fitness排序。")
        except IOError as e:
            logger.error(f"保存战报文件时出错: {e}")

    def run(self):
        logger.info("Alpha 生成器启动 (Google原生API模式)...")
        fields = self.wq.get_data_fields()
        operators = self.wq.get_operators()
        if not fields or not operators:
            logger.error("无法获取字段或操作符，生成器将在60秒后退出。")
            time.sleep(60)
            return

        while True:
            logger.info(f"开始新一轮 Alpha 生成，目标数量: {self.batch_size}")
            alpha_ideas = [self.generate_alpha_idea(fields, operators) for _ in range(self.batch_size)]
            
            valid_ideas = []
            for idea in alpha_ideas:
                if idea and idea.strip() and idea.strip() != ';':
                    valid_ideas.append(idea)
                else:
                    logger.warning(f"过滤掉由AI生成的无效 Alpha: '{idea}'")
            alpha_ideas = valid_ideas

            logger.info(f"成功生成 {len(alpha_ideas)} 个有效的新 Alpha 表达式。")
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
                            checks = is_stats.get("checks", [])
                            passed, failed, pending = 0, 0, 0
                            check_details = []
                            for check in checks:
                                res = check.get("result", "UNKNOWN")
                                if res == "PASS": passed += 1
                                elif res == "FAIL": failed += 1
                                elif res == "PENDING": pending += 1
                                check_details.append(f"{check.get('name')}: {res}")
                            report = {
                                "expression": result.get("regular", {}).get("code"),
                                "alpha_id": result.get("id"),
                                "grade": result.get("grade", "UNKNOWN"),
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "performance": {
                                    "sharpe": is_stats.get("sharpe"),
                                    "fitness": is_stats.get("fitness"),
                                    "turnover": is_stats.get("turnover")
                                },
                                "checks_summary": f"{passed} PASS / {failed} FAIL / {pending} PENDING",
                                "checks_details": check_details
                            }
                            new_reports.append(report)
                            logger.info(f"生成新的Alpha战报: {report['expression']}")
                        except Exception as e:
                            logger.error(f"生成战报时出错: {e}")
                if new_reports:
                    self.save_and_update_reports(new_reports)
            
            sleep_time = 900
            logger.info(f"本轮结束。等待{sleep_time}秒（{sleep_time/60:.1f}分钟）开始下一轮...")
            time.sleep(sleep_time)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator with Google Native API')
    parser.add_argument('--user-id', type=str, required=True)
    parser.add_argument('--api-key', type=str, required=True)
    parser.add_argument('--batch-size', type=int, default=5)
    parser.add_argument('--api-config-path', type=str, default="api_config.json")
    args = parser.parse_args()
    try:
        wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
        generator = AlphaGenerator(wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size)
        generator.run()
    except Exception as e:
        logger.critical(f"启动 Alpha 生成器时发生致命错误: {e}", exc_info=True)