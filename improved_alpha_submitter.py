import requests
import json
import logging
import time
import os
from requests.auth import HTTPBasicAuth
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from datetime import datetime, timedelta

# 配置日志记录器
logger = logging.getLogger(__name__)


class ImprovedAlphaSubmitter:
    def __init__(self, credentials_path: str):
        self.sess = requests.Session()
        # 为所有请求设置更长的超时时间
        self.sess.timeout = (30, 300)  # (连接超时, 读取超时)
        self.setup_auth(credentials_path)

    def setup_auth(self, credentials_path: str) -> None:
        """设置 WorldQuant Brain 的身份验证。"""
        username = os.getenv("WQ_USER_ID")
        password = os.getenv("WQ_API_KEY")
        if not username or not password:
            with open(credentials_path, encoding="utf-8") as f:
                raw = f.read().strip()
            try:
                credentials = json.loads(raw)
                if isinstance(credentials, dict):
                    username = credentials.get("username") or credentials.get("user_id")
                    password = credentials.get("password") or credentials.get("api_key")
                else:
                    username, password = credentials[:2]
            except (json.JSONDecodeError, TypeError, ValueError):
                lines = [line.strip() for line in raw.splitlines() if line.strip()]
                tokens = lines[0].split() if lines else []
                if len(tokens) >= 2:
                    username, password = tokens[0], tokens[1]
                elif len(lines) >= 2:
                    username, password = lines[0], lines[1]
                else:
                    raise ValueError("凭据文件必须是 JSON、同一行的用户名密码或两行凭据")

        if not username or not password:
            raise ValueError("缺少 WorldQuant 用户名或 API 密钥")
        self.sess.auth = HTTPBasicAuth(username, password)

        response = self.sess.post("https://api.worldquantbrain.com/authentication")
        if response.status_code != 201:
            raise Exception(f"身份验证失败: {response.text}")
        logger.info("成功通过 WorldQuant Brain 身份验证")

    def check_hopeful_alphas_count(self, min_count: int = 50) -> bool:
        """检查是否有足够的有希望的 alpha 来开始提交。"""
        hopeful_file = "hopeful_alphas.json"

        if not os.path.exists(hopeful_file):
            logger.info(f"未找到有希望的 alpha 文件 {hopeful_file}")
            return False

        try:
            with open(hopeful_file, "r") as f:
                hopeful_alphas = json.load(f)

            count = len(hopeful_alphas)
            logger.info(f"在 {hopeful_file} 中找到 {count} 个有希望的 alpha")

            if count >= min_count:
                logger.info(
                    f"有足够的有希望的 alpha ({count} >= {min_count})，继续进行提交"
                )
                return True
            else:
                logger.info(
                    f"没有足够的有希望的 alpha ({count} < {min_count})，跳过提交"
                )
                return False

        except Exception as e:
            logger.error(f"读取有希望的 alpha 文件时出错: {str(e)}")
            return False

    def load_hopeful_alphas(self) -> List[Dict]:
        """从 JSON 文件加载有希望的 alpha。"""
        hopeful_file = "hopeful_alphas.json"

        try:
            with open(hopeful_file, "r") as f:
                hopeful_alphas = json.load(f)

            logger.info(
                f"从 {hopeful_file} 加载了 {len(hopeful_alphas)} 个有希望的 alpha"
            )
            return hopeful_alphas

        except Exception as e:
            logger.error(f"加载有希望的 alpha 时出错: {str(e)}")
            return []

    def fetch_successful_alphas(self, offset: int = 0, limit: int = 10) -> Dict:
        """获取具有良好性能指标的未提交成功的 alpha。"""
        url = "https://api.worldquantbrain.com/users/self/alphas"
        params = {
            "limit": limit,
            "offset": offset,
            "status": "UNSUBMITTED",
            "is.fitness>": 1,
            "is.sharpe>": 1.25,
            "order": "-dateCreated",
            "hidden": "false",
        }

        logger.info(f"使用参数获取 alpha: {params}")
        full_url = f"{url}?{'&'.join(f'{k}={v}' for k,v in params.items())}"
        logger.info(f"请求 URL: {full_url}")

        max_retries = 5
        base_delay = 30

        for attempt in range(max_retries):
            try:
                logger.debug(f"尝试 {attempt + 1}/{max_retries} 获取 alpha")
                response = self.sess.get(url, params=params)
                logger.info(f"响应 URL: {response.url}")
                logger.info(f"响应状态码: {response.status_code}")

                if response.status_code == 429:  # Too Many Requests
                    wait_time = int(
                        response.headers.get("Retry-After", base_delay * (2**attempt))
                    )
                    logger.info(f"请求频率受限。等待 {wait_time} 秒...")
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                logger.info(
                    f"成功获取 {len(data.get('results', []))} 个 alpha。总数: {data.get('count', 0)}"
                )
                return data

            except requests.exceptions.Timeout as e:
                logger.warning(f"尝试 {attempt + 1} 超时: {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2**attempt)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"在 {max_retries} 次尝试后由于超时而无法获取 alpha"
                    )
                    return {"count": 0, "results": []}

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"尝试 {attempt + 1} 失败: {str(e)}")
                    wait_time = base_delay * (2**attempt)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"在 {max_retries} 次尝试后无法获取 alpha。最后错误: {e}"
                    )
                    return {"count": 0, "results": []}

        return {"count": 0, "results": []}

    def monitor_submission(self, alpha_id: str, max_timeout_minutes: int = 15) -> Dict:
        """使用改进的超时处理监控提交状态。"""
        url = f"https://api.worldquantbrain.com/alphas/{alpha_id}/submit"

        start_time = time.time()
        max_timeout_seconds = max_timeout_minutes * 60
        base_sleep_time = 5
        max_sleep_time = 60

        attempt = 0

        while (time.time() - start_time) < max_timeout_seconds:
            attempt += 1
            elapsed_minutes = (time.time() - start_time) / 60

            try:
                logger.info(
                    f"监控尝试 {attempt} 对 alpha {alpha_id} (已用时间: {elapsed_minutes:.1f} 分钟)"
                )
                response = self.sess.get(url)
                logger.info(f"响应状态码: {response.status_code}")

                if response.status_code == 404:
                    logger.info(f"Alpha {alpha_id} 已提交或未找到")
                    return {"status": "already_submitted", "alpha_id": alpha_id}

                if response.status_code != 200:
                    logger.error(f"Alpha {alpha_id} 提交失败")
                    logger.error(f"响应状态码: {response.status_code}")
                    logger.error(f"响应文本: {response.text}")
                    return {
                        "status": "failed",
                        "error": response.text,
                        "alpha_id": alpha_id,
                    }

                # 如果响应为空（仍在提交中）
                if not response.text.strip():
                    logger.info(f"Alpha {alpha_id} 仍在提交中，等待中...")
                    # 指数退避策略，设置上限
                    sleep_time = min(
                        base_sleep_time * (1.5 ** (attempt - 1)), max_sleep_time
                    )
                    time.sleep(sleep_time)
                    continue

                # 尝试解析 JSON 响应（提交完成）
                try:
                    data = response.json()
                    logger.info(f"Alpha {alpha_id} 提交完成")
                    return {"status": "success", "data": data, "alpha_id": alpha_id}
                except json.JSONDecodeError:
                    logger.info(
                        f"Alpha {alpha_id} 的响应尚未采用 JSON 格式，继续监控..."
                    )
                    sleep_time = min(
                        base_sleep_time * (1.5 ** (attempt - 1)), max_sleep_time
                    )
                    time.sleep(sleep_time)

            except requests.exceptions.Timeout as e:
                logger.warning(
                    f"监控尝试 {attempt} 对 alpha {alpha_id} 超时: {str(e)}"
                )
                if (time.time() - start_time) < max_timeout_seconds:
                    sleep_time = min(base_sleep_time * (2**attempt), max_sleep_time)
                    logger.info(f"等待 {sleep_time} 秒后重试...")
                    time.sleep(sleep_time)
                else:
                    logger.error(
                        f"监控 alpha {alpha_id} 在 {max_timeout_minutes} 分钟后超时"
                    )
                    return {
                        "status": "timeout",
                        "error": "监控超时",
                        "alpha_id": alpha_id,
                    }

            except Exception as e:
                logger.warning(
                    f"监控尝试 {attempt} 对 alpha {alpha_id} 失败: {str(e)}"
                )
                if (time.time() - start_time) < max_timeout_seconds:
                    sleep_time = min(base_sleep_time * (1.5**attempt), max_sleep_time)
                    time.sleep(sleep_time)
                else:
                    logger.error(
                        f"监控 alpha {alpha_id} 在 {max_timeout_minutes} 分钟后失败"
                    )
                    return {"status": "error", "error": str(e), "alpha_id": alpha_id}

        logger.error(
            f"监控 alpha {alpha_id} 在 {max_timeout_minutes} 分钟后超时"
        )
        return {
            "status": "timeout",
            "error": "监控超时",
            "alpha_id": alpha_id,
        }

    def log_submission_result(self, alpha_id: str, result: Dict) -> None:
        """将提交结果记录到文件。"""
        log_file = "submission_results.json"

        # Load existing results
        existing_results = []
        if os.path.exists(log_file):
            try:
                with open(log_file, "r") as f:
                    existing_results = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"无法解析 {log_file}，重新开始")

        # Add new result
        entry = {
            "alpha_id": alpha_id,
            "timestamp": int(time.time()),
            "datetime": datetime.now().isoformat(),
            "result": result,
        }
        existing_results.append(entry)

        # Save updated results
        with open(log_file, "w") as f:
            json.dump(existing_results, f, indent=2)

        logger.info(f"已记录 alpha {alpha_id} 的提交结果")

    def has_fail_checks(self, alpha: Dict) -> bool:
        """检查 alpha 的检查结果中是否有 FAIL。"""
        checks = alpha.get("checks", [])
        return any(check.get("result") == "FAIL" for check in checks)

    def submit_alpha(self, alpha_id: str) -> bool:
        """提交单个 alpha 并监控其状态。"""
        url = f"https://api.worldquantbrain.com/alphas/{alpha_id}/submit"
        logger.info(f"正在提交 alpha {alpha_id}")
        logger.info(f"请求 URL: {url}")

        max_retries = 3
        base_delay = 10

        for attempt in range(max_retries):
            try:
                # Initial submission
                response = self.sess.post(url)
                logger.info(f"响应状态码: {response.status_code}")

                if response.status_code == 201:
                    logger.info(
                        f"成功提交 alpha {alpha_id}，正在监控状态..."
                    )

                    # 使用更长的超时时间监控提交状态
                    result = self.monitor_submission(alpha_id, max_timeout_minutes=20)
                    if result:
                        self.log_submission_result(alpha_id, result)
                        if result.get("status") in ["success", "already_submitted"]:
                            return True
                        else:
                            logger.error(
                                f"Alpha {alpha_id} 提交失败: {result.get('error', '未知错误')}"
                            )
                            return False
                    else:
                        logger.error(
                            f"监控 alpha {alpha_id} 提交状态失败"
                        )
                        return False

                elif response.status_code == 409:
                    logger.info(f"Alpha {alpha_id} 已提交")
                    return True

                else:
                    logger.error(
                        f"提交 alpha {alpha_id} 失败。状态码: {response.status_code}"
                    )
                    logger.error(f"响应文本: {response.text}")

                    if attempt < max_retries - 1:
                        wait_time = base_delay * (2**attempt)
                        logger.info(f"等待 {wait_time} 秒后重试...")
                        time.sleep(wait_time)
                    else:
                        return False

            except requests.exceptions.Timeout as e:
                logger.warning(
                    f"提交尝试 {attempt + 1} 对 alpha {alpha_id} 超时: {str(e)}"
                )
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2**attempt)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"在 {max_retries} 次尝试后，alpha {alpha_id} 提交超时"
                    )
                    return False

            except Exception as e:
                logger.error(
                    f"提交 alpha {alpha_id} 时出错 (尝试 {attempt + 1}): {str(e)}"
                )
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2**attempt)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    logger.exception("完整堆栈跟踪:")
                    return False

        return False

    def submit_hopeful_alphas(self, batch_size: int = 3) -> None:
        """Submit hopeful alphas from JSON file with improved error handling."""
        logger.info(f"Starting hopeful alphas submission with batch size {batch_size}")

        # Load hopeful alphas
        hopeful_alphas = self.load_hopeful_alphas()
        if not hopeful_alphas:
            logger.info("No hopeful alphas to process")
            return

        # Filter out alphas with FAIL checks
        valid_alphas = [
            alpha for alpha in hopeful_alphas if not self.has_fail_checks(alpha)
        ]
        logger.info(f"Found {len(valid_alphas)} valid alphas after filtering FAILs")

        if not valid_alphas:
            logger.info("No valid alphas to submit")
            return

        # Submit valid alphas in batches
        total_submitted = 0
        consecutive_failures = 0
        max_consecutive_failures = 3

        for i in range(0, len(valid_alphas), batch_size):
            batch = valid_alphas[i : i + batch_size]
            logger.info(
                f"Processing batch {i//batch_size + 1}/{(len(valid_alphas)-1)//batch_size + 1} ({len(batch)} alphas)"
            )

            batch_successes = 0
            for alpha in batch:
                alpha_id = alpha.get("alpha_id")
                if not alpha_id:
                    logger.warning("Alpha missing alpha_id, skipping")
                    continue

                expression = alpha.get("expression", "Unknown")
                metrics = (
                    f"Sharpe: {alpha.get('sharpe', 'N/A')}, "
                    f"Fitness: {alpha.get('fitness', 'N/A')}"
                )
                logger.info(f"Submitting alpha {alpha_id}:")
                logger.info(f"Expression: {expression}")
                logger.info(f"Metrics: {metrics}")

                if self.submit_alpha(alpha_id):
                    batch_successes += 1
                    total_submitted += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

                # Wait between submissions to avoid rate limiting
                time.sleep(30)

            if batch_successes == 0:
                consecutive_failures += 1
                logger.warning(
                    f"Batch failed. Consecutive failures: {consecutive_failures}"
                )

                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"Too many consecutive failures ({consecutive_failures}), stopping submission"
                    )
                    break
            else:
                consecutive_failures = 0

            # Wait between batches
            if i + batch_size < len(valid_alphas):
                logger.info(f"Waiting 120 seconds before next batch...")
                time.sleep(120)

        logger.info(
            f"Hopeful alphas submission complete. Total alphas submitted: {total_submitted}"
        )

        # Clean up hopeful_alphas.json after successful submission
        if total_submitted > 0:
            self.cleanup_hopeful_alphas()

    def cleanup_hopeful_alphas(self):
        """Clean up hopeful_alphas.json after successful submission."""
        hopeful_file = "hopeful_alphas.json"
        backup_file = f"hopeful_alphas_backup_{int(time.time())}.json"

        try:
            # Create backup
            if os.path.exists(hopeful_file):
                import shutil

                shutil.copy2(hopeful_file, backup_file)
                logger.info(f"Created backup: {backup_file}")

            # Clear the file
            with open(hopeful_file, "w") as f:
                json.dump([], f)

            logger.info(f"Cleared {hopeful_file} after successful submission")

        except Exception as e:
            logger.error(f"清理有希望的 alpha 文件时出错: {str(e)}")

    def batch_submit(self, batch_size: int = 3) -> None:
        """Submit alphas in batches with improved error handling."""
        logger.info(f"Starting batch submission with batch size {batch_size}")
        offset = 0
        total_submitted = 0
        consecutive_failures = 0
        max_consecutive_failures = 3

        while True:
            logger.info(f"Fetching batch at offset {offset}")
            response = self.fetch_successful_alphas(offset=offset, limit=batch_size)

            if not response or not response.get("results"):
                logger.info("No more alphas to process")
                break

            results = response["results"]
            if not results:
                logger.info("Empty results batch")
                break

            logger.info(f"Processing batch of {len(results)} alphas...")

            # Filter out alphas with FAIL checks
            valid_alphas = [
                alpha for alpha in results if not self.has_fail_checks(alpha)
            ]
            logger.info(f"Found {len(valid_alphas)} valid alphas after filtering FAILs")

            if not valid_alphas:
                logger.info("No valid alphas in this batch, moving to next")
                offset += batch_size
                continue

            # Submit valid alphas sequentially to avoid overwhelming the API
            batch_successes = 0
            for alpha in valid_alphas:
                alpha_id = alpha["id"]
                expression = alpha["regular"]["code"]
                metrics = (
                    f"Sharpe: {alpha['is']['sharpe']}, "
                    f"Fitness: {alpha['is']['fitness']}"
                )
                logger.info(f"Submitting alpha {alpha_id}:")
                logger.info(f"Expression: {expression}")
                logger.info(f"Metrics: {metrics}")

                if self.submit_alpha(alpha_id):
                    batch_successes += 1
                    total_submitted += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

                # Wait between submissions to avoid rate limiting
                time.sleep(30)

            if batch_successes == 0:
                consecutive_failures += 1
                logger.warning(
                    f"Batch failed. Consecutive failures: {consecutive_failures}"
                )

                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"Too many consecutive failures ({consecutive_failures}), stopping submission"
                    )
                    break
            else:
                consecutive_failures = 0

            if not response.get("next"):
                logger.info("No more pages to process")
                break

            offset += batch_size
            logger.info(f"Waiting 120 seconds before next batch...")
            time.sleep(120)

        logger.info(
            f"Submission process complete. Total alphas submitted: {total_submitted}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Submit successful alphas to WorldQuant Brain with improved timeout handling"
    )
    parser.add_argument(
        "--credentials",
        type=str,
        default="./credential.txt",
        help="Path to credentials file (default: ./credential.txt)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="Number of alphas to submit per batch (default: 3)",
    )
    parser.add_argument(
        "--interval-hours",
        type=int,
        default=24,
        help="Hours to wait between submission runs (default: 24)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    parser.add_argument(
        "--auto-mode",
        action="store_true",
        help="Run in automated mode (single run, no continuous loop)",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=20,
        help="Maximum timeout for submission monitoring in minutes (default: 20)",
    )
    parser.add_argument(
        "--min-hopeful-count",
        type=int,
        default=50,
        help="Minimum count of hopeful alphas required to start submission (default: 50)",
    )
    parser.add_argument(
        "--use-hopeful-file",
        action="store_true",
        help="Use hopeful_alphas.json file instead of fetching from API",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("improved_alpha_submitter.log"),
        ],
    )

    if not os.path.exists(args.credentials):
        logger.error(f"Credentials file not found: {args.credentials}")
        return 1

    interval_seconds = args.interval_hours * 3600

    try:
        if args.auto_mode:
            # Single run in auto mode
            logger.info(
                f"Starting single submission run at {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # Check minimum hopeful alphas count
            if not args.use_hopeful_file:
                submitter = ImprovedAlphaSubmitter(args.credentials)
                submitter.batch_submit(batch_size=args.batch_size)
            else:
                submitter = ImprovedAlphaSubmitter(args.credentials)
                if submitter.check_hopeful_alphas_count(args.min_hopeful_count):
                    submitter.submit_hopeful_alphas(batch_size=args.batch_size)
                else:
                    logger.info("Insufficient hopeful alphas, skipping submission")

            logger.info("Single submission run complete")
        else:
            # Continuous loop mode
            while True:
                logger.info(
                    f"Starting submission run at {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                try:
                    submitter = ImprovedAlphaSubmitter(args.credentials)

                    if args.use_hopeful_file:
                        if submitter.check_hopeful_alphas_count(args.min_hopeful_count):
                            submitter.submit_hopeful_alphas(batch_size=args.batch_size)
                        else:
                            logger.info(
                                "Insufficient hopeful alphas, skipping submission"
                            )
                    else:
                        submitter.batch_submit(batch_size=args.batch_size)

                    logger.info(
                        f"Submission run complete. Waiting {args.interval_hours} hours before next run..."
                    )
                except Exception as e:
                    logger.error(f"Error during submission run: {str(e)}")
                    logger.exception("Full traceback:")

                # Sleep until next run
                next_run = time.time() + interval_seconds
                next_run_time = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(next_run)
                )
                logger.info(f"Next run scheduled for: {next_run_time}")
                time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal, exiting gracefully...")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        return 1


if __name__ == "__main__":
    exit(main())
