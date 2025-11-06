# --- wq_client.py v13.3.3 (修复 I/O Storm + Sleep-Lock Bug) ---
# WorldQuant API 交互模块

import logging
import json
import time
import requests
import threading
import math 
from requests.adapters import HTTPAdapter, Retry

# v13.3.1: 我们现在依赖 utils 里的 FileLock，所以 utils 的正确性至关重要
from utils import load_system_config, save_system_config

# 获取一个专用的 logger
logger = logging.getLogger(__name__)

class WorldQuant:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.base_url = "https://api.worldquantbrain.com"
        self.session = self._create_resilient_session()
        
        self.auth_lock = threading.Lock() 
        self.request_lock = threading.Lock() 
        
        # --- v13.0: 看门狗 B (WQ 令牌桶) 状态 ---
        self.wq_limiter_lock = threading.Lock() # v13.3.2: 保护对 *内存中* 令牌桶的读写
        self.wq_token_bucket = [] # 存储请求的时间戳 (float)
        
        # --- v13.3.3: I/O Storm 修复 ---
        self.success_counter = 0 # 内存中的成功计数器
        self.success_counter_lock = threading.Lock() # 保护内存计数器
        self.SUCCESS_WRITE_BATCH_SIZE = 20 # 每成功 20 次才写入一次磁盘
        # --- v13.3.3 结束 ---


        self._authenticate()
        self.default_settings = {
            'instrumentType': 'EQUITY', 'universe': 'TOP3000', 'region': 'USA',
            'delay': 1, 'decay': 4, 'neutralization': 'SUBINDUSTRY',
            'truncation': 0.1, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
            'nanHandling': 'ON', 'language': 'FASTEXPR', 'visualization': False,
        }

    # --- v13.3.2: 修复 "sleep-while-holding-lock" Bug ---
    def _acquire_wq_token(self):
        """
        (v13.3.2) 线程安全地获取一个 WQ API 令牌。
        """
        
        # --- 步骤 1: 检查是否需要休眠 (在锁内) ---
        wait_duration = 0
        try:
            # (v13.3.1) utils.py 中的 FileLock 负责跨进程同步
            config = load_system_config() # <-- Miner Worker 在这里被饿死
            limiter_config = config.get("wq_api_limiter", {})

            # v13.3.2: 修复键名
            cooldown_key = "wq_429_cooldown_seconds"
            wait_after_429 = limiter_config.get(cooldown_key, 60)

            tpm_limit = limiter_config.get("current_tpm_limit", 60)
            last_failure_ts = limiter_config.get("last_failure_timestamp", 0)

            now = time.time()
                
            # 1. 检查是否处于 429 强制冷却期
            time_since_failure = now - last_failure_ts
            if time_since_failure < wait_after_429:
                wait_duration = (last_failure_ts + wait_after_429) - now
                logger.warning(f"[Watchdog B] 处于 429 冷却期。强制休眠 {wait_duration:.1f} 秒...")
            
            # --- 仅在锁内操作内存中的 wq_token_bucket ---
            with self.wq_limiter_lock:
                # 2. 清理过期的令牌 (60 秒前)
                self.wq_token_bucket = [ts for ts in self.wq_token_bucket if now - ts < 60]

                # 3. 检查令牌桶是否已满 (仅当不在 429 冷却时)
                if wait_duration == 0 and len(self.wq_token_bucket) >= tpm_limit:
                    oldest_token_ts = self.wq_token_bucket[0] if self.wq_token_bucket else now
                    wait_duration = 60.0 - (now - oldest_token_ts) + 0.1 
                    
                    logger.info(f"[Watchdog B] 速率限制器激活 (TPM: {tpm_limit})。等待 {wait_duration:.2f} 秒...")
            
        except Exception as e:
            logger.error(f"[Watchdog B] _acquire_wq_token (步骤 1: 检查) 发生严重错误: {e}", exc_info=True)
            wait_duration = 5.0

        # --- 步骤 2: 执行休眠 (在锁外) ---
        # (v13.3.2) 关键修复：休眠时 *不* 持有任何锁！
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

# --- v13.3.4: 移除动态TPM增长，回归静态TPM ---
    def _record_wq_success(self):
        """ 
        (v13.3.4) 动态TPM增长已被禁用，以确保稳定性。
        此函数现在什么也不做 (No-op)。
        TPM 现在是一个由用户在 settings.html 中设置的静态值。
        """
        pass # <-- 彻底移除所有 I/O 风暴的来源
    # --- v13.3.4 修复结束 ---


    def _record_wq_failure_429(self):
        """ 
        (v13.0) 记录一次 429 失败。
        (v13.3.3) 失败是关键事件，必须立即写入磁盘。
        """
        # (v13.3.1) utils.py 中的 FileLock 保证了并发安全
        try:
            config = load_system_config()
            limiter_config = config.get("wq_api_limiter", {})

            current_tpm = limiter_config.get("current_tpm_limit", 60)
            min_tpm = limiter_config.get("min_tpm_limit", 15)
            decrement_factor = limiter_config.get("tpm_decrement_factor_on_429", 0.75)
            
            # v13.3.2: 修复配置键名
            cooldown_key = "wq_429_cooldown_seconds"
            wait_after_429 = limiter_config.get(cooldown_key, 60)
            
            new_tpm = math.floor(current_tpm * decrement_factor)
            new_tpm = max(new_tpm, min_tpm) 
            
            now = time.time()
            config["wq_api_limiter"]["current_tpm_limit"] = new_tpm
            config["wq_api_limiter"]["last_failure_timestamp"] = now
            
            logger.critical(f"[Watchdog B] 检测到 WQ 429 Rate Limit！")
            logger.critical(f"[Watchdog B] TPM 限制从 {current_tpm} 大幅降低至 {new_tpm}。")
            logger.critical(f"[Watchdog B] 触发 {wait_after_429} 秒强制冷却期。")

            # 关键：清空内存令牌桶，强制所有等待的线程重新评估冷却
            with self.wq_limiter_lock:
                self.wq_token_bucket = [] 

            # 立即写入磁盘
            if not save_system_config(config):
                logger.error("[Watchdog B] 保存 system_config (failure) 失败！")
            
            # v13.3.3: 立即重置内存中的成功计数器，防止脏写
            with self.success_counter_lock:
                self.success_counter = 0

        except Exception as e:
             logger.error(f"[Watchdog B] _record_wq_failure_429 发生错误: {e}", exc_info=True)


    def _create_resilient_session(self):
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('https://', adapter)
        logger.info("创建了带有3次重试机制的API会话。")
        return session

    def _authenticate(self):
        # (此函数保持 v13.3.2 的逻辑不变)
        with self.auth_lock:
            self._acquire_wq_token()
            
            url = f"{self.base_url}/authentication"
            logger.info("Attempting WorldQuant Brain authentication...")
            try:
                self.session.auth = (self.user_id, self.api_key)
                response = self.session.post(url, timeout=30)
                response.raise_for_status()
                
                self._record_wq_success() # v13.3.3: 这是一个轻量级的内存操作
                
                logger.info("WorldQuant Brain authentication successful.")
            except requests.exceptions.RequestException as e:
                
                if e.response is not None and e.response.status_code == 429:
                    logger.critical(f"认证时遭遇 WQ 429 Rate Limit！")
                    self._record_wq_failure_429() # v13.3.3: 这是一个重量级的磁盘操作
                
                logger.error(f"WorldQuant Brain authentication failed: {e}")
                self.session.auth = None
                raise 

    def _make_request(self, method, url, **kwargs):
        """
        (v13.3.3) 集成所有修复
        """
        
        # 1. (v13.3.2) 获取令牌 (此函数会阻塞/休眠，但不再锁死其他线程)
        self._acquire_wq_token()

        with self.request_lock:
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status() 
                
                self._record_wq_success() # v13.3.3: 轻量级内存操作
                return response
                
            except requests.exceptions.RequestException as e:
                
                if e.response is not None and e.response.status_code == 429:
                    self._record_wq_failure_429() # v13.3.3: 重量级磁盘操作
                    raise e 
                
                if e.response is not None and e.response.status_code == 401:
                    logger.warning(f"Request failed with 401 Unauthorized for {method} {url}. Attempting re-authentication...")
                    try:
                        self._authenticate() 
                        logger.info(f"Re-authentication successful. Retrying the original request to {url}...")
                        
                        self._acquire_wq_token()
                        
                        response = self.session.request(method, url, **kwargs)
                        response.raise_for_status()
                        
                        self._record_wq_success() # v13.3.3: 轻量级内存操作
                        return response
                        
                    except requests.exceptions.RequestException as auth_e:
                        if auth_e.response is not None and auth_e.response.status_code == 429:
                            self._record_wq_failure_429() # v13.3.3: 重量级磁盘操作
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

    # --- v9.4.2: Remove problematic advXX fields ---
    def get_data_fields(self):
        # (此函数保持不变)
        logger.info("正在使用筛选后的核心及高级数据字段列表...")
        safe_fields = [
            "open", "high", "low", "close", "volume", "vwap",
            "cap", "returns", "turnover", "beta", "momentum",
            "adv20", 
            "buy_turnover", "sell_turnover", "indneutral_beta"
        ]
        logger.info(f"成功加载 {len(safe_fields)} 个筛选后的数据字段。")
        return safe_fields

    # --- v13.0: Updated get_operators with 429 handling ---
    def get_operators(self):
        # (此函数保持不变)
        url = f"{self.base_url}/operators"
        try:
            response = self._make_request('GET', url, timeout=60) 
            data = response.json()
            op_list = data.get('results', []) if isinstance(data, dict) else data
            operators = [str(op) for op in op_list]
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

    # --- v13.0: Updated test_alpha with 429 handling ---
    def test_alpha(self, alpha_expression: str, custom_settings: dict = None):
        # (此函数保持不变)
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