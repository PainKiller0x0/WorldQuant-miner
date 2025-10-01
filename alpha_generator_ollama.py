import argparse
import logging
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.auth import HTTPBasicAuth

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

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user_id, api_key)
        self._authenticate()

    def _authenticate(self):
        url = f"{self.base_url}/authentication"
        try:
            response = self.session.post(url)
            response.raise_for_status()
            logger.info("WorldQuant Brain authentication successful.")
        except requests.exceptions.RequestException as e:
            logger.error(f"WorldQuant Brain authentication failed: {e}")
            raise
    def get_data_fields(self):
            # =================================================================
            # == 终极解决方案 V3.0：硬编码标准新手数据字段 ==
            # =================================================================
            # 不再调用API，直接使用最可能的新手教学字段列表。
            
            logger.info("正在使用硬编码的标准新手数据字段列表 (TUTORIAL模式)")
            
            tutorial_fields = [
                # --- 核心量价数据 ---
                "open", 
                "high", 
                "low", 
                "close", 
                "volume",
                "vwap",  # 成交量加权平均价
                
                # --- 常用衍生数据 ---
                "adv5",  # 过去5天的日均成交量
                "adv10",
                "adv20",
                "adv30",
                "adv60",
                "adv120",
                "turnover", # 换手率
                
                # --- 可能包含的基础财务数据 ---
                "market_cap", # 市值
            ]
            
            logger.info(f"成功加載 {len(tutorial_fields)} 個手動設定的數據字段。")
            return tutorial_fields
    # def get_data_fields(self):
    #     url = f"{self.base_url}/data-fields"
    #     params = {"limit": 100, "offset": 0}
    #     try:
    #         logger.info(f"正在從 {url} 獲取數據字段，參數: {params}")
    #         response = self.session.get(url, params=params)
    #         response.raise_for_status()
    #         data = response.json()
    #         # 更具防御性的解析代码
    #         if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and 'id' in data[0]:
    #              fields = [field['id'] for field in data if 'id' in field]
    #         elif isinstance(data, dict) and 'results' in data and isinstance(data['results'], list):
    #              fields = [field['id'] for field in data['results'] if 'id' in field]
    #         else:
    #              # 假设它是一个简单的字符串列表
    #              fields = [str(item) for item in data]

    #         logger.info(f"成功獲取 {len(fields)} 個數據字段。")
    #         return fields
    #     except requests.exceptions.RequestException as e:
    #         # 打印出服务器返回的原始文本内容
    #         error_content = e.response.text if e.response else "No response content"
    #         logger.error(f"Failed to get data fields: {e} - Response: {error_content}")
    #         return []

    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            # 更具防御性的解析代码
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and 'name' in data[0]:
                operators = [op['name'] for op in data if 'name' in op]
            else:
                # 假设它是一个简单的字符串列表
                operators = [str(item) for item in data]
    
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            error_content = e.response.text if hasattr(e, 'response') and e.response else "No response content"
            logger.error(f"Failed to get operators: {e} - Response: {error_content}")
            return []
    
    def test_alpha(self, alpha_expression):
        url = f"{self.base_url}/alphas"
        payload = {"code": alpha_expression}
        try:
            response = self.session.post(url, json=payload, timeout=1800) # 30 min timeout
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to test alpha '{alpha_expression}': {e}")
            return None

class AlphaGenerator:
    def __init__(self, wq, model, batch_size=30, temperature=0.8):
        self.wq = wq
        self.model = model
        self.batch_size = batch_size
        self.temperature = temperature
        self.ollama_url = os.getenv("OLLAMA_HOST", "http://ollama:11434") + "/api/generate"
        self.tested_alphas_file = "tested_alphas.json"
        self.tested_alphas = self.load_tested_alphas()
        logger.info(f"成功加載 {len(self.tested_alphas)} 個已測試過的 Alpha 記錄。")

    def load_tested_alphas(self):
        try:
            if os.path.exists(self.tested_alphas_file):
                with open(self.tested_alphas_file, 'r') as f:
                    data = json.load(f)
                    return set(data)
            return set()
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加載已測試的 alphas 文件時出錯: {e}, 將創建一個新的記錄文件。")
            return set()

    def save_tested_alpha(self, alpha_expression):
        self.tested_alphas.add(alpha_expression)
        try:
            with open(self.tested_alphas_file, 'w') as f:
                json.dump(list(self.tested_alphas), f)
        except IOError as e:
            logger.error(f"保存已測試的 alpha 文件時出錯: {e}")

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
        payload = {"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": self.temperature}}
        try:
            response = requests.post(self.ollama_url, json=payload, timeout=1800)
            response.raise_for_status()
            idea = response.json()['response'].strip().replace('`', '')
            return f"{idea.rstrip(';')};"
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to generate alpha idea from Ollama: {e}")
            return None

    def test_alpha(self, alpha):
        clean_alpha = alpha.strip()
        if not clean_alpha: return None
        if clean_alpha in self.tested_alphas:
            logger.info(f"跳過已測試的 Alpha: {clean_alpha}")
            return None
        logger.info(f"正在測試新 Alpha: {clean_alpha}")
        try:
            result = self.wq.test_alpha(clean_alpha)
            return result
        except Exception as e:
            logger.error(f"測試 Alpha '{clean_alpha}' 時出錯: {e}")
            return None
        finally:
            self.save_tested_alpha(clean_alpha)

    def run(self):
        logger.info("Alpha 生成器啟動...")
        fields = self.wq.get_data_fields()
        operators = self.wq.get_operators()
        if not fields or not operators:
            logger.error("無法獲取字段或操作符，生成器將在60秒後退出。")
            time.sleep(60)
            return

        while True:
            logger.info(f"開始新一輪 Alpha 生成，目標數量: {self.batch_size}")
            alpha_ideas = []
            with ThreadPoolExecutor(max_workers=1) as executor:
                futures = {executor.submit(self.generate_alpha_idea, fields, operators) for _ in range(self.batch_size)}
                for future in as_completed(futures):
                    idea = future.result()
                    if idea: alpha_ideas.append(idea)
            
            logger.info(f"成功生成 {len(alpha_ideas)} 個新 Alpha 表達式。")
            if not alpha_ideas:
                logger.info("本輪未生成有效 Alpha，等待60秒...")
                time.sleep(60)
                continue

            hopeful_alphas = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_result = {executor.submit(self.test_alpha, alpha): alpha for alpha in alpha_ideas}
                for future in as_completed(future_to_result):
                    result = future.result()
                    if result and result.get('is_hopeful'):
                        logger.info(f"發現一個有希望的 Alpha: {result.get('regular', {}).get('code', '')}")
                        hopeful_alphas.append(result)

            if hopeful_alphas: self.save_hopeful_alphas(hopeful_alphas)
            logger.info(f"本輪結束，發現了 {len(hopeful_alphas)} 個有希望的 Alpha。等待300秒開始下一輪...")
            time.sleep(300)
    
    def save_hopeful_alphas(self, hopeful_alphas):
        file_path = 'hopeful_alphas.json'
        existing_data = {}
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                try: existing_data = json.load(f)
                except json.JSONDecodeError: pass
        
        new_alphas_dict = {alpha.get('id', alpha.get('regular', {}).get('code')): alpha for alpha in hopeful_alphas}
        merged_data = {**existing_data, **new_alphas_dict}
        
        with open(file_path, 'w') as f:
            json.dump(merged_data, f, indent=4)
        logger.info(f"已將 {len(new_alphas_dict)} 個新的有希望的 Alpha 保存/更新到 {file_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alpha Generator using Ollama')
    parser.add_argument('--user-id', type=str, required=True)
    parser.add_argument('--api-key', type=str, required=True)
    parser.add_argument('--model', type=str, required=True)
    parser.add_argument('--batch-size', type=int, default=30)
    args = parser.parse_args()

    try:
        wq_client = WorldQuant(user_id=args.user_id, api_key=args.api_key)
        generator = AlphaGenerator(wq_client, model=args.model, batch_size=args.batch_size)
        generator.run()
    except Exception as e:
        logger.critical(f"啟動 Alpha 生成器時發生致命錯誤: {e}")
