# --- wq_client.py ---
# WorldQuant API 交互模块

import logging
import json
import time
import requests
import threading
from requests.adapters import HTTPAdapter, Retry

# 获取一个专用的 logger
# 它将继承由 alpha_generator_ollama.py 中 setup_logging() 配置的根 logger
logger = logging.getLogger(__name__)

class WorldQuant:
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
                # v7.3 修改: 429 错误会在这里被捕获并抛出，由 __main__ 中的启动逻辑处理
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                raise

    def get_data_fields(self):
        # --- v7.5 优化: 扩展数据字段列表 ---
        logger.info("正在使用扩展的、针对高级用户的官方核心数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap",
            "cap", "returns", "turnover", "beta", "momentum",
            "adv20", "adv40", "adv60", "adv80", "adv120",
            "buy_turnover", "sell_turnover", "indneutral_beta"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个核心及高级数据字段。")
        return safe_fields
        # --- v7.5 结束 ---

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
            # v7.3 修改: 检查 429
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"获取操作符时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
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
            # --- v7.3 修改: 捕获 429 Rate Limit ---
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                logger.critical(f"提交模拟时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
            # --- v7.3 结束 ---
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
                    # --- v7.6 修改: 返回完整的错误 JSON ---
                    return result_data
                    # --- v7.6 结束 ---
                else:
                    logger.debug(f"Alpha '{alpha_expression}' 仍在模拟中... 状态: {status}")
                    time.sleep(10)
            except requests.exceptions.RequestException as e:
                # --- v7.3 修改: 捕获 429 Rate Limit ---
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                    logger.critical(f"轮询结果时检测到 WorldQuant 429 Rate Limit: {e}")
                    return "RATE_LIMIT"
                # --- v7.3 结束 ---
                logger.error(f"轮询结果失败: {e}，将在15秒后重试...")
                time.sleep(15)
            except Exception as e:
                logger.error(f"处理轮询结果时发生未知错误: {e}")
                return None

        logger.warning(f"Alpha '{alpha_expression}' 模拟超时（超过 {POLLING_TIMEOUT/60:.0f} 分钟）。")
        return "TIMEOUT"