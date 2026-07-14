# --- wq_client.py v13.3.10 (最终稳定版) ---
# (保留 429 安全刹车, 移除 成功I/O风暴)
import logging
import json
import time
import requests
import threading
import math 
from requests.adapters import HTTPAdapter, Retry

from utils import load_system_config, save_system_config

logger = logging.getLogger(__name__)

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        
        self.auth_lock = threading.Lock() 
        self.request_lock = threading.Lock() 
        
        self.wq_limiter_lock = threading.Lock() 
        self.wq_token_bucket = [] 

        # --- v13.3.12: 添加“低 I/O”成功计数器 ---
        self.successful_requests_since_last_write = 0
        self.success_io_lock = threading.Lock() # 保护计数器和文件I/O
        # --- v13.3.12 结束 ---

        self._authenticate()
        self.default_settings = {
            'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA',
            'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY',
            'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
            'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False,
        }

    # --- v13.3.2: "持锁休眠" Bug 修复 (保留) ---
    def _acquire_wq_token(self):
        """
        (v13.3.10) 令牌桶核心逻辑 (保留)。
        它现在会正确地读取 v13.3.10 写入的 429 时间戳。
        """
        
        wait_duration = 0
        try:
            # (v13.3.1) utils.py 中的 FileLock 负责跨进程同步
            config = load_system_config()
            limiter_config = config.get("wq_api_limiter", {})

            cooldown_key = "wq_429_cooldown_seconds"
            wait_after_429 = limiter_config.get(cooldown_key, 60)

            tpm_limit = limiter_config.get("current_tpm_limit", 60)
            last_failure_ts = limiter_config.get("last_failure_timestamp", 0)

            now = time.time()
                
            # 1. 检查是否处于 429 强制冷却期
            # (v13.3.10: _record_wq_failure_429 现在会正确更新时间戳, 
            #  所以这个检查会生效, 从而打破死循环)
            time_since_failure = now - last_failure_ts
            if time_since_failure < wait_after_429:
                wait_duration = (last_failure_ts + wait_after_429) - now
                logger.warning(f"[Watchdog B] 处于 429 冷却期。强制休眠 {wait_duration:.1f} 秒...")
            
            with self.wq_limiter_lock:
                # 2. 清理过期的令牌
                self.wq_token_bucket = [ts for ts in self.wq_token_bucket if now - ts < 60]

                # 3. 检查令牌桶是否已满
                if wait_duration == 0 and len(self.wq_token_bucket) >= tpm_limit:
                    oldest_token_ts = self.wq_token_bucket[0] if self.wq_token_bucket else now
                    wait_duration = 60.0 - (now - oldest_token_ts) + 0.1 
                    
                    logger.info(f"[Watchdog B] 速率限制器激活 (TPM: {tpm_limit})。等待 {wait_duration:.2f} 秒...")
            
        except Exception as e:
            logger.error(f"[Watchdog B] _acquire_wq_token (步骤 1: 检查) 发生严重错误: {e}", exc_info=True)
            wait_duration = 5.0

        # --- 步骤 2: 执行休眠 (在锁外) ---
        if wait_duration > 0:
            time.sleep(wait_duration)

        # --- 步骤 3: 添加令牌 (在锁内) ---
        try:
            with self.wq_limiter_lock:
                now = time.time()
                self.wq_token_bucket = [ts for ts in self.wq_token_bucket if now - ts < 60]
                self.wq_token_bucket.append(now)
        except Exception as e:
             logger.error(f"[Watchdog B] _acquire_wq_token (步骤 3: 添加) 发生严重错误: {e}", exc_info=True)
             time.sleep(1)
    # --- v13.3.2 修复结束 ---

    # --- v13.3.12: 恢复“低 I/O”的 TPM 增长 ---
    def _record_wq_success(self):
        """ 
        (v13.3.12) 恢复 TPM 增长, 但使用计数器来防止 I/O 风暴。
        每 20 次成功请求才触发一次磁盘写入。
        """
        try:
            with self.success_io_lock:
                self.successful_requests_since_last_write += 1
                
                # 仅在累积 20 次成功后才执行 I/O
                if self.successful_requests_since_last_write < 20:
                    return # 快速退出, 不执行 I/O

                # --- 达到 20 次，执行 I/O ---
                self.successful_requests_since_last_write = 0 # 重置计数器
                
                # (v13.3.1) utils.py 中的 FileLock 负责跨进程同步
                config = load_system_config()
                limiter_config = config.get("wq_api_limiter", {})

                current_tpm = limiter_config.get("current_tpm_limit", 60)
                max_tpm = limiter_config.get("max_tpm_limit", 200)
                increment = limiter_config.get("tpm_increment_on_success", 1) # (v13.3.12: 恢复使用此配置)

                if current_tpm < max_tpm:
                    new_tpm = min(current_tpm + increment, max_tpm)
                    config["wq_api_limiter"]["current_tpm_limit"] = new_tpm
                    
                    # 立即写入磁盘 (低频)
                    if not save_system_config(config):
                        logger.error("[Watchdog B] 保存 system_config (success) 失败！")
                    else:
                        logger.info(f"[Watchdog B] TPM 限制在 20 次成功后，从 {current_tpm} 增加到 {new_tpm}。")
                
        except Exception as e:
            logger.error(f"[Watchdog B] _record_wq_success 发生错误: {e}", exc_info=True)
    # --- v13.3.12 修复结束 ---

    # --- v13.3.10: 恢复安全刹车 (必须) ---
    def _record_wq_failure_429(self):
        """ 
        (v13.3.10) 恢复 429 失败处理。
        这对于写入 last_failure_timestamp 至关重要，以防止 Livelock。
        """
        # (v13.3.1) utils.py 中的 FileLock 保证了并发安全
        try:
            # --- v13.3.12: 重置成功计数器 ---
            with self.success_io_lock:
                self.successful_requests_since_last_write = 0
            # --- v13.3.12 结束 ---
            config = load_system_config()
            limiter_config = config.get("wq_api_limiter", {})

            current_tpm = limiter_config.get("current_tpm_limit", 60)
            min_tpm = limiter_config.get("min_tpm_limit", 15)
            decrement_factor = limiter_config.get("tpm_decrement_factor_on_429", 0.75)
            
            cooldown_key = "wq_429_cooldown_seconds"
            wait_after_429 = limiter_config.get(cooldown_key, 60)
            
            new_tpm = math.floor(current_tpm * decrement_factor)
            new_tpm = max(new_tpm, min_tpm) 
            
            now = time.time()
            config["wq_api_limiter"]["current_tpm_limit"] = new_tpm
            config["wq_api_limiter"]["last_failure_timestamp"] = now # <-- 修复 Livelock 的关键
            
            logger.critical(f"[Watchdog B] 检测到 WQ 429 Rate Limit！")
            logger.critical(f"[Watchdog B] TPM 限制从 {current_tpm} 大幅降低至 {new_tpm}。")
            logger.critical(f"[Watchdog B] 触发 {wait_after_429} 秒强制冷却期。")

            with self.wq_limiter_lock:
                self.wq_token_bucket = [] 

            # 立即写入磁盘 (这是必须的)
            if not save_system_config(config):
                logger.error("[Watchdog B] 保存 system_config (failure) 失败！")

        except Exception as e:
             logger.error(f"[Watchdog B] _record_wq_failure_429 发生错误: {e}", exc_info=True)
    # --- v13.3.10 修复结束 ---


    def _create_resilient_session(self):
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('https://', adapter)
        logger.info("创建了带有3次重试机制的API会话。")
        return session

    def _authenticate(self):
        # (此函数保持 v13.3.4 的逻辑不变)
        with self.auth_lock:
            self._acquire_wq_token()
            
            url = f"{self.base_url}/authentication"
            logger.info("Attempting WorldQuant Brain authentication...")
            try:
                self.session.auth = (self.user_id, self.api_key)
                response = self.session.post(url, timeout=30)
                response.raise_for_status()
                
                self._record_wq_success() # (v13.3.4: 这是一个空操作)
                
                logger.info("WorldQuant Brain authentication successful.")
            except requests.exceptions.RequestException as e:
                
                if e.response is not None and e.response.status_code == 429:
                    logger.critical(f"认证时遭遇 WQ 429 Rate Limit！")
                    self._record_wq_failure_429() # (v13.3.10: 恢复功能)
                
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                self.session.auth = None
                raise 

    def _make_request(self, method, url, **kwargs):
            """
            (v13.3.14) 修复并发踩踏 (Thundering Herd) Livelock
            """
            
            # 关键修复：必须先获取“请求锁”，确保同一时间只有一个线程
            # 可以尝试获取 API 令牌。这可以序列化所有 worker 的请求。
            with self.request_lock: 
            
                # 关键修复：在锁 *内部* 获取令牌
                self._acquire_wq_token() 
    
                try:
                    response = self.session.request(method, url, **kwargs)
                    response.raise_for_status() 
                    
                    self._record_wq_success() # (v13.3.12: 低 I/O 恢复)
                    return response
                    
                except requests.exceptions.RequestException as e:
                    
                    if e.response is not None and e.response.status_code == 429:
                        self._record_wq_failure_429() # (v13.3.10: 恢复功能)
                        raise e 
                    
                    if e.response is not None and e.response.status_code == 401:
                        logger.warning(f"Request failed with 401 Unauthorized for {method} {url}. Attempting re-authentication...")
                        try:
                            # 注意：_authenticate() 会自己获取令牌，但它也在 self.request_lock 内部
                            self._authenticate() 
                            logger.info(f"Re-authentication successful. Retrying the original request to {url}...")
                            
                            # 重试也必须在锁内部获取令牌
                            self._acquire_wq_token()
                            
                            response = self.session.request(method, url, **kwargs)
                            response.raise_for_status()
                            
                            self._record_wq_success() # (v13.3.12: 低 I/O 恢复)
                            return response
                            
                        except requests.exceptions.RequestException as auth_e:
                            if auth_e.response is not None and auth_e.response.status_code == 429:
                                self._record_wq_failure_429() # (v13.3.10: 恢复功能)
                                raise auth_e 
                            
                            logger.error(f"Re-authentication or retry failed: {auth_e}")
                            raise auth_e 
                        except Exception as general_auth_e:
                             logger.error(f"An unexpected error occurred during re-authentication: {general_auth_e}")
                             raise general_auth_e 
                    else:
                        raise e
                except Exception as general_e:
                     logger.error(f"An unexpected error occurred during the request to {url}: {general_e}")
                     raise general_e

# --- v13.3.21: 移除 buy_turnover/sell_turnover (毒井修复) ---
    def get_data_fields(self):
        """
        (v13.3.21) 进一步净化 safe_fields 列表。
        日志显示 "buy_turnover" 也是 unknown variable。
        """
        logger.info("正在使用筛选后的核心及高级数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap",
            "cap", "returns", 
            # "turnover",  # (v13.3.20 移除)
            "beta", 
            # "momentum",  # (v13.3.20 移除)
            "adv20", 
            # "buy_turnover", "sell_turnover", # (v13.3.21 移除: WQ API 不识别)
            "indneutral_beta"
        ]
        # 现在应该是 11 个字段
        logger.info(f"成功加载 {len(safe_fields)} 个 (v13.3.21) 筛选后的数据字段。")
        return safe_fields
    # --- v13.3.21 修复结束 ---

    # (get_operators 保持不变)
    def get_operators(self):
        url = f"{self.base_url}/operators"
        try:
            response = self._make_request('GET', url, timeout=60) 
            data = response.json()
            op_list = data.get('results', []) if isinstance(data, dict) else data
            operators = []
            for op in op_list:
                if isinstance(op, dict):
                    name = op.get('name') or op.get('operator') or op.get('id')
                    if name:
                        operators.append(str(name))
                elif op:
                    operators.append(str(op))
            logger.info(f"成功獲取 {len(operators)} 個操作符。")
            return operators
        except requests.exceptions.RequestException as e:
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

    # (test_alpha 保持不变)
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
            submit_response = self._make_request('POST', submit_url, json=payload, timeout=120)

            progress_url = submit_response.headers.get('location')
            if not progress_url:
                logger.error(f"提交模拟任务后，未能从Header获取 location。")
                return None
            logger.info(f"成功提交模拟任务，进度URL: {progress_url}")

        except requests.exceptions.RequestException as e:
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
                    time.sleep(10) 

            except requests.exceptions.RequestException as e:
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
