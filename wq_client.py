# --- wq_client.py v13.0 (Dual Watchdog - Watchdog B) ---
# WorldQuant API 交互模块

import logging
import json
import time
import requests
import threading
import math # v13.0: 新增
from requests.adapters import HTTPAdapter, Retry

# --- v13.0: 新增导入 ---
from utils import load_system_config, save_system_config
# --- v13.0 结束 ---

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        
        # --- v13.0: 锁具 (保留 v9.4.1 的锁, 新增 v13.0 的锁) ---
        self.auth_lock = threading.Lock() # Lock for authentication process
        self.request_lock = threading.Lock() # v9.4.1: Lock for 401 re-auth race conditions
        
        # --- v13.0: 看门狗 B (WQ 令牌桶) 状态 ---
        self.wq_limiter_lock = threading.Lock() # 保护对 system_config 和令牌桶的读写
        self.wq_token_bucket = [] # 存储请求的时间戳 (float)
        # --- v13.0 结束 ---

        self._authenticate()
        self.default_settings = {
            'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA',
            'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY',
            'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
            'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False,
        }

    # --- v13.0: 看门狗 B (WQ 令牌桶) 核心逻辑 ---
    def _acquire_wq_token(self):
        """
        (v13.0) 线程安全地获取一个 WQ API 令牌。
        如果速率超过动态 TPM 限制，将阻塞 (time.sleep)。
        如果刚发生过 429，将强制冷却。
        """
        with self.wq_limiter_lock:
            try:
                config = load_system_config()
                limiter_config = config.get("wq_api_limiter", {})
                
                tpm_limit = limiter_config.get("current_tpm_limit", 60)
                last_failure_ts = limiter_config.get("last_failure_timestamp", 0)
                wait_after_429 = limiter_config.get("seconds_to_wait_after_429", 60)
                
                now = time.time()
                
                # 1. 检查是否处于 429 强制冷却期
                if now - last_failure_ts < wait_after_429:
                    wait_duration = (last_failure_ts + wait_after_429) - now
                    logger.warning(f"[Watchdog B] 处于 429 冷却期。强制休眠 {wait_duration:.1f} 秒...")
                    time.sleep(wait_duration)
                    now = time.time() # 更新当前时间

                # 2. 清理过期的令牌 (60 秒前)
                self.wq_token_bucket = [ts for ts in self.wq_token_bucket if now - ts < 60]

                # 3. 检查令牌桶是否已满
                if len(self.wq_token_bucket) >= tpm_limit:
                    # 桶已满，计算需要等待多长时间
                    oldest_token_ts = self.wq_token_bucket[0]
                    wait_duration = 60.0 - (now - oldest_token_ts) + 0.1 # +0.1s 缓冲
                    
                    logger.info(f"[Watchdog B] 速率限制器激活 (TPM: {tpm_limit})。等待 {wait_duration:.2f} 秒...")
                    time.sleep(wait_duration)
                    
                    # 再次清理 (因为我们睡了一会)
                    now = time.time()
                    self.wq_token_bucket = [ts for ts in self.wq_token_bucket if now - ts < 60]
                
                # 4. 添加当前请求的令牌
                self.wq_token_bucket.append(now)
                
            except Exception as e:
                logger.error(f"[Watchdog B] _acquire_wq_token 发生严重错误: {e}", exc_info=True)
                # 发生未知错误时，保守起见，休眠5秒
                time.sleep(5)

    def _record_wq_success(self):
        """ (v13.0) 记录一次成功的 API 调用，动态增加 TPM 限制。"""
        with self.wq_limiter_lock:
            try:
                config = load_system_config()
                limiter_config = config.get("wq_api_limiter", {})
                
                current_tpm = limiter_config.get("current_tpm_limit", 60)
                max_tpm = limiter_config.get("max_tpm_limit", 200)
                increment = limiter_config.get("tpm_increment_on_success", 1)
                
                new_tpm = min(current_tpm + increment, max_tpm)
                
                if new_tpm != current_tpm:
                    config["wq_api_limiter"]["current_tpm_limit"] = new_tpm
                    if not save_system_config(config):
                        logger.error("[Watchdog B] 保存 system_config (success) 失败！")
                    else:
                        logger.info(f"[Watchdog B] API 调用成功。TPM 限制提升至: {new_tpm}")
            except Exception as e:
                 logger.error(f"[Watchdog B] _record_wq_success 发生错误: {e}", exc_info=True)

    def _record_wq_failure_429(self):
        """ (v13.0) 记录一次 429 失败，动态降低 TPM 限制并强制冷却。"""
        with self.wq_limiter_lock:
            try:
                config = load_system_config()
                limiter_config = config.get("wq_api_limiter", {})

                current_tpm = limiter_config.get("current_tpm_limit", 60)
                min_tpm = limiter_config.get("min_tpm_limit", 15)
                decrement_factor = limiter_config.get("tpm_decrement_factor_on_429", 0.75)
                wait_after_429 = limiter_config.get("seconds_to_wait_after_429", 60)
                
                # 计算新的 TPM
                new_tpm = math.floor(current_tpm * decrement_factor)
                new_tpm = max(new_tpm, min_tpm) # 不能低于下限
                
                now = time.time()
                config["wq_api_limiter"]["current_tpm_limit"] = new_tpm
                config["wq_api_limiter"]["last_failure_timestamp"] = now
                
                logger.critical(f"[Watchdog B] 检测到 WQ 429 Rate Limit！")
                logger.critical(f"[Watchdog B] TPM 限制从 {current_tpm} 大幅降低至 {new_tpm}。")
                logger.critical(f"[Watchdog B] 触发 {wait_after_429} 秒强制冷却期。")

                # 关键：清空令牌桶，强制所有等待的线程重新评估冷却
                self.wq_token_bucket = [] 

                if not save_system_config(config):
                    logger.error("[Watchdog B] 保存 system_config (failure) 失败！")
            except Exception as e:
                 logger.error(f"[Watchdog B] _record_wq_failure_429 发生错误: {e}", exc_info=True)
    # --- v13.0 结束 ---


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
            # v13.0: 认证请求也需要令牌
            self._acquire_wq_token()
            
            url = f"{self.base_url}/authentication"
            logger.info("Attempting WorldQuant Brain authentication...")
            try:
                self.session.auth = (self.user_id, self.api_key)
                response = self.session.post(url, timeout=30)
                response.raise_for_status()
                
                self._record_wq_success() # v13.0: 认证成功也是一次 success
                
                logger.info("WorldQuant Brain authentication successful.")
            except requests.exceptions.RequestException as e:
                
                # v13.0: 处理认证时的 429
                if e.response is not None and e.response.status_code == 429:
                    logger.critical(f"认证时遭遇 WQ 429 Rate Limit！")
                    self._record_wq_failure_429()
                
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                self.session.auth = None
                raise # Re-raise the exception to be handled by the caller

    def _make_request(self, method, url, **kwargs):
        """
        v13.0: 重构，集成看门狗 B (令牌桶)
        """
        
        # 1. (v13.0) 获取令牌 (此函数会阻塞/休眠，直到令牌可用或冷却结束)
        self._acquire_wq_token()

        # 2. (v9.4.1) 获取 401 重试锁
        with self.request_lock:
            try:
                # 3. 执行请求
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                
                # 4. (v13.0) 记录成功
                self._record_wq_success()
                
                return response
                
            except requests.exceptions.RequestException as e:
                
                # 5. (v13.0) 处理 429
                if e.response is not None and e.response.status_code == 429:
                    self._record_wq_failure_429()
                    raise e # 重新引发 429 异常，由调用者 (test_alpha/get_operators) 处理
                
                # 6. (v9.4.1) 处理 401
                if e.response is not None and e.response.status_code == 401:
                    logger.warning(f"Request failed with 401 Unauthorized for {method} {url}. Attempting re-authentication...")
                    try:
                        self._authenticate() # Attempt to re-authenticate (内部已包含令牌获取/成功逻辑)
                        logger.info(f"Re-authentication successful. Retrying the original request to {url}...")
                        
                        # 7. (v13.0) 重试请求也需要新令牌
                        self._acquire_wq_token()
                        
                        response = self.session.request(method, url, **kwargs)
                        response.raise_for_status()
                        
                        # 8. (v13.0) 重试成功
                        self._record_wq_success()
                        return response
                        
                    except requests.exceptions.RequestException as auth_e:
                         # 9. (v13.0) 检查重试是否也失败 (例如 429)
                        if auth_e.response is not None and auth_e.response.status_code == 429:
                            self._record_wq_failure_429()
                            raise auth_e # 重新引发 429
                        
                        logger.error(f"Re-authentication or retry failed: {auth_e}")
                        raise auth_e # Raise the authentication or retry error
                    except Exception as general_auth_e:
                         logger.error(f"An unexpected error occurred during re-authentication: {general_auth_e}")
                         raise general_auth_e 
                else:
                    # For other request exceptions (non-401, non-429), just re-raise them
                    raise e
            except Exception as general_e:
                 logger.error(f"An unexpected error occurred during the request to {url}: {general_e}")
                 raise general_e


    # --- v9.4.2: Remove problematic advXX fields ---
    def get_data_fields(self):
        # (v13.0: 此函数是硬编码的，不调用 API，因此不需要令牌)
        logger.info("正在使用筛选后的核心及高级数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap",
            "cap", "returns", "turnover", "beta", "momentum",
            "adv20", 
            "buy_turnover", "sell_turnover", "indneutral_beta"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个筛选后的数据字段。")
        return safe_fields
    # --- v9.4.2 End ---

    # --- v13.0: Updated get_operators with 429 handling ---
    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            # Use the helper method (v13.0: _make_request 内部处理 429)
            response = self._make_request('GET', url, timeout=60) 
            data = response.json()
            op_list = data.get('results', []) if isinstance(data, dict) else data
            operators = [str(op) for op in op_list]
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except requests.exceptions.RequestException as e:
            # v13.0: _make_request 会在 429 时重新引发异常，我们在这里捕获它
            if e.response is not None and e.response.status_code == 429:
                logger.critical(f"获取操作符时检测到 WorldQuant 429 Rate Limit (已由 Watchdog B 处理)。")
                return "RATE_LIMIT"
            
            logger.error(f"Failed to get operators after potential retries: {e}")
            return [] 
        except json.JSONDecodeError as e:
             logger.error(f"Failed to decode JSON response for operators: {e}")
             return []
        except Exception as e: 
             logger.error(f"An unexpected error occurred in get_operators: {e}", exc_info=True)
             return []
    # --- v13.0 End ---

    # --- v13.0: Updated test_alpha with 429 handling ---
    def test_alpha(self, alpha_expression: str, custom_settings: dict = None):
        submit_url = f"{self.base_url}/simulations"
        current_settings = self.default_settings.copy()
        if custom_settings:
            valid_custom_settings = {k: v for k, v in custom_settings.items() if k in self.default_settings}
            current_settings.update(valid_custom_settings)
            if len(valid_custom_settings) < len(custom_settings):
                 ignored_keys = set(custom_settings.keys()) - set(valid_custom_settings.keys())
                 logger.warning(f"Ignoring invalid/unknown settings keys: {ignored_keys}")
            logger.info(f"使用自定义参数进行测试: {current_settings}")
        else:
             logger.info(f"使用默认参数进行测试: {current_settings}")


        payload = {'type': 'REGULAR', 'regular': alpha_expression, 'settings': current_settings}

        progress_url = None 
        try:
            # Use helper for submission (v13.0)
            submit_response = self._make_request('POST', submit_url, json=payload, timeout=120)

            progress_url = submit_response.headers.get('location')
            if not progress_url:
                logger.error(f"提交模拟任务后，未能从Header获取 location。")
                return None
            logger.info(f"成功提交模拟任务，进度URL: {progress_url}")

        except requests.exceptions.RequestException as e:
            # v13.0: 捕获 429
            if e.response is not None and e.response.status_code == 429:
                logger.critical(f"提交模拟时检测到 WorldQuant 429 Rate Limit (已由 Watchdog B 处理)。")
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
                # Use helper for polling (v13.0)
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
                    
                    # (v13.0) Final get also needs a token
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
                    time.sleep(10) # (v13.0: 保留这个轮询间隔)

            except requests.exceptions.RequestException as e:
                # v13.0: 捕获 429
                if e.response is not None and e.response.status_code == 429:
                    logger.critical(f"轮询结果时检测到 WorldQuant 429 Rate Limit (已由 Watchdog B 处理)。")
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
    # --- v13.0 End ---