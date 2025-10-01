# === 粘贴替换掉 alpha_generator_ollama.py 的所有旧代码 ===
import argparse
import logging
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from openai import OpenAI # 使用 OpenAI 库来兼容第三方服务

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

# --- WorldQuant API 部分 (不变) ---
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
        # 最终版 V7.0: 吸取服务器的教训，只使用最核心、最不可能出错的“量价”数据字段
        logger.info("正在使用最终版、最核心的数据字段列表")
        
        core_fields = [
            # 只有这些，才是永恒的、不可动摇的真理
            "open", 
            "high", 
            "low", 
            "close", 
            "volume",
            "vwap",
        ]
        
        logger.info(f"成功加載 {len(core_fields)} 個核心数据字段。")
        return core_fields

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
        # 最终版 V7.0: 增加网络容忍度
        
        submit_url = f"{self.base_url}/simulations"
        payload = {
            'type': 'REGULAR',
            'regular': alpha_expression,
            'settings': {
                'instrumentType': 'EQUITY',
                'universe': 'TOP3000',
                'region': 'USA',
                'delay': 1,
                'decay': 4,
                'neutralization': 'SUBINDUSTRY',
                'truncation': 0.1,
                'pasteurization': 'ON',
                'unitHandling': 'VERIFY',
                'nanHandling': 'ON',
                'language': 'FASTEXPR',
                'visualization': False,
            }
        }
        
        try:
            submit_response = self.session.post(submit_url, json=payload, timeout=120) # 提交超时也放宽一点
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
            logger.error(f"第1步：提交模拟任务失败 '{alpha_expression}': {e} - Response: {error_content}")
            return None

        polling_start_time = time.time()
        
        while time.time() - polling_start_time < 600:
            try:
                # --- 这里是关键的修改 ---
                # 把单次轮询的超时时间从60秒延长到120秒
                poll_response = self.session.get(progress_url, timeout=120) 
                
                if poll_response.status_code == 401:
                    logger.warning("认证可能已过期，正在尝试重新认证...")
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
            
# --- 全新的、简洁的 AlphaGenerator ---
class AlphaGenerator:
    def __init__(self, wq, api_config_path, batch_size=2):
        self.wq = wq
        self.batch_size = batch_size
        
        try:
            with open(api_config_path, 'r') as f:
                config = json.load(f)
            
            self.client = OpenAI(
                api_key=config['api_key'],
                base_url=config['base_url']
            )
            logger.info(f"API client initialized for endpoint: {config['base_url']}")
        except Exception as e:
            logger.critical(f"加载 API 配置或初始化客户端失败: {e}")
            raise

        self.tested_alphas_file = "tested_alphas.json"
        self.tested_alphas = self.load_tested_alphas()

    def load_tested_alphas(self):
            # 增加保险丝：如果发现是文件夹，就删了重建
            if os.path.isdir(self.tested_alphas_file):
                logger.warning(f"'{self.tested_alphas_file}' 是一个文件夹，正在删除并重建为空文件。")
                import shutil
                shutil.rmtree(self.tested_alphas_file)
                open(self.tested_alphas_file, 'a').close()

            try:
                if os.path.exists(self.tested_alphas_file) and os.path.isfile(self.tested_alphas_file):
                    with open(self.tested_alphas_file, 'r') as f:
                        content = f.read()
                        if content: return set(json.loads(content))
                return set()
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 tested_alphas.json 出错: {e}, 创建新文件。")
                return set()

    def save_tested_alpha(self, alpha_expression):
        self.tested_alphas.add(alpha_expression)
        try:
            with open(self.tested_alphas_file, 'w') as f:
                json.dump(list(self.tested_alphas), f)
        except IOError as e:
            logger.error(f"保存 tested_alphas.json 出错: {e}")

    def generate_alpha_idea(self, fields, operators):
        field_list = ", ".join(fields)
        operator_list = ", ".join(operators)
        prompt = f"""
        You are a Quantitative Analyst creating alphas for WorldQuant.
        Generate a single, novel alpha expression using the fields and operators provided.
        Available Fields: {field_list}
        Available Operators: {operator_list}
        Your response MUST ONLY be the alpha expression itself, with no explanation or code block markers.
        Example: `rank(corr(adv20, high, 5));`
        New Alpha Expression:
        """
        try:
            chat_completion = self.client.chat.completions.create(
                model="gemini-2.5-flash", # ClawCloud会处理好模型映射，我们用一个通用名字
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.8,
            )
            idea = chat_completion.choices[0].message.content.strip().replace('`', '')
            return f"{idea.rstrip(';')};"
        except Exception as e:
            logger.error(f"从 ClawCloud API 生成 Alpha 失败: {e}")
            return None

    def test_alpha(self, alpha):
        clean_alpha = alpha.strip()
        if not clean_alpha: return None
        if clean_alpha in self.tested_alphas:
            logger.info(f"跳过已测试的 Alpha: {clean_alpha}")
            return None
        logger.info(f"正在测试新 Alpha: {clean_alpha}")
        try:
            result = self.wq.test_alpha(clean_alpha)
            return result
        finally:
            self.save_tested_alpha(clean_alpha)
            
    def save_hopeful_alphas(self, hopeful_alphas):
        file_path = 'hopeful_alphas.json'
        existing_data = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                    if content: existing_data = json.loads(content)
            except (IOError, json.JSONDecodeError): pass
        
        new_alphas_dict = {alpha.get('id', alpha.get('regular', {}).get('code')): alpha for alpha in hopeful_alphas}
        merged_data = {**existing_data, **new_alphas_dict}
        
        with open(file_path, 'w') as f:
            json.dump(merged_data, f, indent=4)
        logger.info(f"已將 {len(new_alphas_dict)} 個新的有希望的 Alpha 保存/更新到 {file_path}")

    def run(self):
        logger.info("Alpha 生成器启动 (ClawCloud API 模式)...")
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
                hopeful_alphas = []
                with ThreadPoolExecutor(max_workers=10) as executor:
                    future_to_result = {executor.submit(self.test_alpha, alpha): alpha for alpha in alpha_ideas}
                    for future in as_completed(future_to_result):
                        result = future.result()
                        if result and result.get('is_hopeful'):
                            logger.info(f"发现一个有希望的 Alpha: {result.get('regular', {}).get('code', '')}")
                            hopeful_alphas.append(result)
                if hopeful_alphas: self.save_hopeful_alphas(hopeful_alphas)

            logger.info(f"本轮结束。等待600秒（10分钟）开始下一轮...")
            time.sleep(600)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator using a generic API endpoint')
    parser.add_argument('--user-id', type=str, required=True)
    parser.add_argument('--api-key', type=str, required=True) # WQ's key
    parser.add_argument('--batch-size', type=int, default=2)
    parser.add_argument('--api-config-path', type=str, default="api_config.json")
    args = parser.parse_args()

    try:
        wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
        generator = AlphaGenerator(wq_client, api_config_path=args.api_config_path, batch_size=args.batch_size)
        generator.run()
    except Exception as e:
        logger.critical(f"启动 Alpha 生成器时发生致命错误: {e}", exc_info=True)