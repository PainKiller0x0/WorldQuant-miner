# --- wq_client.py v9.4.2 (Remove problematic advXX fields) ---
# WorldQuant API 交互模块

import logging
import json
import time
import requests
import threading
from requests.adapters import HTTPAdapter, Retry

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        self.auth_lock = threading.Lock() # Lock for authentication process
        self.request_lock = threading.Lock() # v9.4.1: Lock for general requests to prevent race conditions during re-auth
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
        # Use the dedicated auth_lock here
        with self.auth_lock:
            url = f"{self.base_url}/authentication"
            logger.info("Attempting WorldQuant Brain authentication...") # Add log
            try:
                # Set auth credentials directly on the session for subsequent requests
                self.session.auth = (self.user_id, self.api_key)
                # Make the authentication request itself using the configured session auth
                response = self.session.post(url, timeout=30)
                response.raise_for_status()
                logger.info("WorldQuant Brain authentication successful.")
            except requests.exceptions.RequestException as e:
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                # Clear potentially stale auth if failed
                self.session.auth = None
                raise # Re-raise the exception to be handled by the caller

    def _make_request(self, method, url, **kwargs):
        """
        v9.4.1: Helper method to make requests with re-authentication logic.
        Uses request_lock to prevent multiple threads trying to re-authenticate simultaneously.
        """
        with self.request_lock:
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                return response
            except requests.exceptions.RequestException as e:
                # Check if the error is due to 401 Unauthorized
                if e.response is not None and e.response.status_code == 401:
                    logger.warning(f"Request failed with 401 Unauthorized for {method} {url}. Attempting re-authentication...")
                    try:
                        self._authenticate() # Attempt to re-authenticate
                        # Retry the request once after successful re-authentication
                        logger.info(f"Re-authentication successful. Retrying the original request to {url}...")
                        response = self.session.request(method, url, **kwargs)
                        response.raise_for_status()
                        return response
                    except requests.exceptions.RequestException as auth_e:
                        logger.error(f"Re-authentication failed: {auth_e}")
                        raise auth_e # Raise the authentication error
                    except Exception as general_auth_e:
                         logger.error(f"An unexpected error occurred during re-authentication: {general_auth_e}")
                         raise general_auth_e # Raise unexpected error
                else:
                    # For other request exceptions (non-401), just re-raise them
                    raise e
            except Exception as general_e:
                 logger.error(f"An unexpected error occurred during the request to {url}: {general_e}")
                 raise general_e # Raise unexpected error


    # --- v9.4.2: Remove problematic advXX fields ---
    def get_data_fields(self):
        logger.info("正在使用筛选后的核心及高级数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap",
            "cap", "returns", "turnover", "beta", "momentum",
            "adv20", 
            # "adv40", # Commented out due to API errors
            # "adv60", # Commented out due to API errors
            # "adv80", # Commented out due to API errors
            # "adv120",# Commented out due to API errors
            "buy_turnover", "sell_turnover", "indneutral_beta"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个筛选后的数据字段。")
        return safe_fields
    # --- v9.4.2 End ---

    # --- v9.4.1: Updated get_operators with robust re-authentication ---
    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            # Use the helper method for the request
            response = self._make_request('GET', url, timeout=60) # Increased timeout slightly
            data = response.json()
            op_list = data.get('results', []) if isinstance(data, dict) else data
            operators = [str(op) for op in op_list]
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except requests.exceptions.RequestException as e:
            # Handle potential 429 specifically if _make_request didn't catch it or re-raised
            if e.response is not None and e.response.status_code == 429:
                logger.critical(f"获取操作符时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
            # Log other request exceptions raised by _make_request (including failed re-auth)
            logger.error(f"Failed to get operators after potential retries: {e}")
            return [] # Return empty list on failure
        except json.JSONDecodeError as e:
             logger.error(f"Failed to decode JSON response for operators: {e}")
             return []
        except Exception as e: # Catch any other unexpected errors
             logger.error(f"An unexpected error occurred in get_operators: {e}", exc_info=True)
             return []
    # --- v9.4.1 End ---

    # --- v9.4.1: Updated test_alpha to use _make_request ---
    def test_alpha(self, alpha_expression: str, custom_settings: dict = None):
        submit_url = f"{self.base_url}/simulations"
        current_settings = self.default_settings.copy()
        if custom_settings:
            # v9.4.2: Ensure settings used are aligned with available keys in default_settings
            # This prevents potentially invalid settings from being sent
            valid_custom_settings = {k: v for k, v in custom_settings.items() if k in self.default_settings}
            current_settings.update(valid_custom_settings)
            if len(valid_custom_settings) < len(custom_settings):
                 ignored_keys = set(custom_settings.keys()) - set(valid_custom_settings.keys())
                 logger.warning(f"Ignoring invalid/unknown settings keys: {ignored_keys}")
            logger.info(f"使用自定义参数进行测试: {current_settings}") # Log the final settings used
        else:
             logger.info(f"使用默认参数进行测试: {current_settings}")


        payload = {'type': 'REGULAR', 'regular': alpha_expression, 'settings': current_settings}

        progress_url = None # Initialize progress_url
        try:
            # Use helper for submission
            submit_response = self._make_request('POST', submit_url, json=payload, timeout=120)

            progress_url = submit_response.headers.get('location')
            if not progress_url:
                logger.error(f"提交模拟任务后，未能从Header获取 location。")
                return None
            logger.info(f"成功提交模拟任务，进度URL: {progress_url}")

        except requests.exceptions.RequestException as e:
            if e.response is not None and e.response.status_code == 429:
                logger.critical(f"提交模拟时检测到 WorldQuant 429 Rate Limit: {e}")
                return "RATE_LIMIT"
            error_content = "No response body"
            if e.response is not None:
                try: error_content = e.response.json()
                except json.JSONDecodeError: error_content = e.response.text
            logger.error(f"提交模拟任务失败 '{alpha_expression}' (after potential retries): {e} - Response: {error_content}")
            return None
        except Exception as e:
             logger.error(f"提交模拟任务时发生意外错误 '{alpha_expression}': {e}", exc_info=True)
             return None


        # --- Polling Logic ---
        POLLING_TIMEOUT = 1800
        polling_start_time = time.time()

        while time.time() - polling_start_time < POLLING_TIMEOUT:
            try:
                # Use helper for polling
                poll_url = progress_url
                if not poll_url.startswith('http'):
                    poll_url = f"{self.base_url}{poll_url}" 

                poll_response = self._make_request('GET', poll_url, timeout=120)
                result_data = poll_response.json()
                status = result_data.get("status")

                if status == "COMPLETE":
                    alpha_id = result_data.get("alpha")
                    if not alpha_id:
                        logger.error(f"模拟完成，但未找到 alpha id。 Data: {result_data}")
                        return None 
                    
                    final_alpha_url = f"{self.base_url}/alphas/{alpha_id}"
                    final_response = self._make_request('GET', final_alpha_url, timeout=60) 
                    final_data = final_response.json()
                    logger.info(f"Alpha '{alpha_id}' 模拟完成。")
                    return final_data

                elif status == "ERROR":
                    logger.error(f"Alpha 模拟出错，服务器返回的完整错误报告: {result_data}")
                    return result_data 
                else:
                    logger.debug(f"Alpha '{alpha_expression}' 仍在模拟中... 状态: {status}")
                    time.sleep(10) # Wait before next poll

            except requests.exceptions.RequestException as e:
                if e.response is not None and e.response.status_code == 429:
                    logger.critical(f"轮询结果时检测到 WorldQuant 429 Rate Limit: {e}")
                    return "RATE_LIMIT"
                logger.error(f"轮询结果失败 (after potential retries): {e}，将在15秒后重试...")
                time.sleep(15)
            except json.JSONDecodeError as e:
                 logger.error(f"轮询结果时解析 JSON 失败: {e}，将在15秒后重试...")
                 time.sleep(15)
            except Exception as e:
                logger.error(f"处理轮询结果时发生未知错误: {e}", exc_info=True)
                return None 

        logger.warning(f"Alpha '{alpha_expression}' 模拟超时（超过 {POLLING_TIMEOUT/60:.0f} 分钟）。")
        return "TIMEOUT"
    # --- v9.4.1 End ---