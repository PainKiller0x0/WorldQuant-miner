import argparse
import requests
import json
import os
import time
import logging
import schedule
from datetime import datetime, timedelta
from typing import List, Dict
from requests.auth import HTTPBasicAuth
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
from dataclasses import dataclass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("alpha_orchestrator.log")],
)
logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """Information about a model in the fleet."""

    name: str
    size_mb: int
    priority: int  # Lower number = higher priority (used first)
    description: str


class ModelFleetManager:
    """Manages a fleet of models with automatic downgrading on VRAM issues."""

    def __init__(self, ollama_url: str = "http://localhost:11434"):
        self.ollama_url = ollama_url
        self.current_model_index = 0
        self.vram_error_count = 0
        self.max_vram_errors = 3  # Number of VRAM errors before downgrading

        # Model fleet ordered by priority (first = highest priority / preferred)
        # Prioritize DeepSeek-R1 8B by default; keep smaller models as fallbacks
        self.model_fleet = [
            ModelInfo(
                "qwen3:0.6b", 700, 0, "Qwen3 0.6B - Fastest model"
            ),
            ModelInfo(
                "qwen3:1.7b", 1400, 1, "Qwen3:1.5b - New preferred fast model"
            ),
            ModelInfo(
		      "qwen3:4b", 4096, 2,"Qwen3:4b - my preferred model",
	        ),
            ModelInfo(
                "deepseek-r1:8b",5200,2,"DeepSeek-R1 8B - Preferred default reasoning model",
            ),
            ModelInfo(
                "llama3.2:3b", 2048, 3, "Llama 3.2 3B - Stable fallback (low VRAM)"
            ),
            # Intentionally keep a much smaller reasoning fallback and an emergency tiny model
            ModelInfo(
                "deepseek-r1:1.5b", 1100, 4,"DeepSeek-R1 1.5B - Small reasoning fallback",
            ),
            ModelInfo("phi3:mini", 2200, 5, "Phi3 mini - Emergency fallback"),
        ]

        # State file to persist current model selection
        self.state_file = "model_fleet_state.json"
        self.load_state()

    def load_state(self):
        """Load the current model state from file."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                    self.current_model_index = state.get("current_model_index", 0)
                    self.vram_error_count = state.get("vram_error_count", 0)
                    logger.info(
                        f"Loaded state: model_index={self.current_model_index}, vram_errors={self.vram_error_count}"
                    )
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
            self.current_model_index = 0
            self.vram_error_count = 0

        # Auto-select the best available model on startup
        self.auto_select_available_model()

    def auto_select_available_model(self):
        return
        """Automatically select the best available model on startup."""
        best_available_model = self.select_best_available_model()

        # Find the index of the best available model in our fleet
        for i, model_info in enumerate(self.model_fleet):
            if model_info.name == best_available_model:
                if i != self.current_model_index:
                    logger.info(
                        f"Auto-switching to available model: {best_available_model}"
                    )
                    self.current_model_index = i
                    self.save_state()
                break
        else:
            # If the best available model is not in our fleet, keep current index
            logger.info(
                f"Best available model {best_available_model} not in fleet, keeping current selection"
            )

    def save_state(self):
        """Save the current model state to file."""
        try:
            state = {
                "current_model_index": self.current_model_index,
                "vram_error_count": self.vram_error_count,
                "current_model": self.get_current_model().name,
                "timestamp": time.time(),
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
            logger.info(f"Saved state: {state}")
        except Exception as e:
            logger.error(f"Could not save state: {e}")

    def get_current_model(self) -> ModelInfo:
        """Get the current model in use."""
        if self.current_model_index >= len(self.model_fleet):
            self.current_model_index = len(self.model_fleet) - 1
        return self.model_fleet[self.current_model_index]

    def get_available_models(self) -> List[str]:
        """Get list of available models via Ollama API."""
        try:
            response = requests.get(f"{self.ollama_url}/api/tags")
            if response.status_code == 200:
                models_data = response.json()
                return [model["name"] for model in models_data.get("models", [])]
            else:
                logger.error(f"Failed to get available models: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Error getting available models: {e}")
            return []

    def select_best_available_model(self) -> str:
        """Select the best available model from the fleet, prioritizing larger models if available."""
        available_models = self.get_available_models()

        # Try to find the best available model from our fleet
        for model_info in self.model_fleet:
            if model_info.name in available_models:
                logger.info(f"Selected available model: {model_info.name}")
                return model_info.name

        # If no models from our fleet are available, use the first available model
        if available_models:
            logger.warning(
                f"No preferred models available, using: {available_models[0]}"
            )
            return available_models[0]

        # If no models are available at all, return the default
        logger.error("No models available, using default: llama3.2:3b")
        return "llama3.2:3b"

    def ensure_model_available(self, model_name: str) -> bool:
        """Check if a specific model is available, without downloading."""
        available_models = self.get_available_models()

        if model_name in available_models:
            logger.info(f"Model {model_name} is available")
            return True

        logger.warning(f"Model {model_name} is not available (skipping auto-download)")
        return False

    def detect_vram_error(self, log_line: str) -> bool:
        """Detect VRAM recovery timeout errors in log lines."""
        vram_error_indicators = [
            "gpu VRAM usage didn't recover within timeout",
            "VRAM usage didn't recover",
            "gpu memory exhausted",
            "CUDA out of memory",
            "GPU memory allocation failed",
            'msg="gpu VRAM usage didn\'t recover within timeout"',
            "level=WARN source=sched.go",
        ]

        return any(
            indicator.lower() in log_line.lower() for indicator in vram_error_indicators
        )

    def handle_vram_error(self) -> bool:
        """处理 VRAM 错误，降级到更小的模型，但不会从固定的较小模型切换走"""
        self.vram_error_count += 1
        logger.warning(
            f"检测到 VRAM 错误! 计数: {self.vram_error_count}/{self.max_vram_errors}"
        )

        # If current model is the preferred small model, do not auto-downgrade to other tags
        if self.get_current_model().name == "llama3.2:3b":
            logger.info("Pinned to llama3.2:3b; skipping auto-downgrade.")
            self.save_state()
            return False

        if self.vram_error_count >= self.max_vram_errors:
            return self.downgrade_model()

        self.save_state()
        return False

    def downgrade_model(self) -> bool:
        """降级到模型队列中下一个可用的较小模型"""
        # 找到比当前模型小的下一个可用模型
        current_model = self.get_current_model()
        available_models = self.get_available_models()

        # 寻找更小的可用模型
        for i in range(self.current_model_index + 1, len(self.model_fleet)):
            candidate_model = self.model_fleet[i]
            if candidate_model.name in available_models:
                old_model = current_model
                self.current_model_index = i
                new_model = self.get_current_model()

                logger.warning(
                    f"降级模型: {old_model.name} -> {new_model.name}"
                )

                # 重置 VRAM 错误计数
                self.vram_error_count = 0

                # 保存状态
                self.save_state()

                # 更新 alpha 生成器配置
                self.update_alpha_generator_config(new_model.name)

                logger.info(f"成功降级到 {new_model.name}")
                return True

        # 如果没有找到更小的可用模型，触发应用重置
        logger.error("未找到更小的可用模型用于降级！")
        return self.trigger_application_reset()

    def trigger_application_reset(self) -> bool:
        """当 VRAM 问题持续存在且使用最小模型时，触发完整的应用重置"""
        try:
            logger.warning("由于持续的 VRAM 问题，正在触发应用重置")

            # 重置到首选的小模型（队列重新排序后的索引 0）
            self.current_model_index = 0
            self.vram_error_count = 0
            self.save_state()

            # 更新配置以使用首选模型
            self.update_alpha_generator_config(self.get_current_model().name)

            logger.info("应用重置完成 - 已固定到首选小模型")
            return True

        except Exception as e:
            logger.error(f"应用重置过程中出错: {e}")
            return False

    def update_alpha_generator_config(self, model_name: str):
        """更新 alpha 生成器配置以使用新模型"""
        try:
            # 更新 alpha_generator_ollama.py 中的默认模型
            with open("alpha_generator_ollama.py", "r") as f:
                content = f.read()

            # 替换默认模型
            content = content.replace(
                "default='llama3.2:8b'", f"default='{model_name}'"
            )
            content = content.replace(
                "getattr(self, 'model_name', 'llama3.2:8b')",
                f"getattr(self, 'model_name', '{model_name}')",
            )

            with open("alpha_generator_ollama.py", "w") as f:
                f.write(content)

            logger.info(f"已更新 alpha 生成器配置以使用 {model_name}")
        except Exception as e:
            logger.error(f"更新 alpha 生成器配置时出错: {e}")

    def get_fleet_status(self) -> Dict:
        """获取模型队列的当前状态"""
        current_model = self.get_current_model()
        available_models = self.get_available_models()

        return {
            "current_model": {
                "name": current_model.name,
                "size_mb": current_model.size_mb,
                "description": current_model.description,
                "index": self.current_model_index,
            },
            "vram_error_count": self.vram_error_count,
            "max_vram_errors": self.max_vram_errors,
            "available_models": available_models,
            "fleet_size": len(self.model_fleet),
            "can_downgrade": self.current_model_index < len(self.model_fleet) - 1,
        }

    def reset_to_largest_model(self):
        """重置到模型队列中最大的可用模型"""
        # 找到最大的可用模型
        available_models = self.get_available_models()

        for i, model_info in enumerate(self.model_fleet):
            if model_info.name in available_models:
                self.current_model_index = i
                self.vram_error_count = 0
                self.save_state()
                logger.info(f"重置到最大的可用模型: {model_info.name}")
                return self.update_alpha_generator_config(model_info.name)

        # 如果没有首选模型可用，使用第一个可用模型
        if available_models:
            logger.warning(
                f"没有首选模型可用，使用: {available_models[0]}"
            )
            return True

        logger.error("没有可用于重置的模型")
        return False


class AlphaOrchestrator:
    def __init__(
        self, credentials_path: str, ollama_url: str = "http://localhost:11434"
    ):
        self.sess = requests.Session()
        self.credentials_path = credentials_path
        self.ollama_url = ollama_url
        self.setup_auth(credentials_path)
        self.last_submission_date = None
        self.提交日志文件 = "提交日志.json"
        self.load_submission_history()

        # Concurrency control
        self.max_concurrent_simulations = 3
        self.simulation_semaphore = threading.Semaphore(self.max_concurrent_simulations)
        self.running = True
        self.generator_process = None
        self.miner_process = None

        # Model fleet management
        self.model_fleet_manager = ModelFleetManager(ollama_url)
        self.vram_monitoring_active = False
        self.vram_monitor_thread = None

        # Restart mechanism
        self.restart_interval = 86400  # 24 hours in seconds
        self.last_restart_time = time.time()
        self.restart_thread = None

    def setup_auth(self, credentials_path: str) -> None:
        """Set up authentication with WorldQuant Brain."""
        logger.info(f"Loading credentials from {credentials_path}")
        with open(credentials_path) as f:
            credentials = json.load(f)

        username, password = credentials
        self.sess.auth = HTTPBasicAuth(username, password)

        logger.info("Authenticating with WorldQuant Brain...")
        response = self.sess.post("https://api.worldquantbrain.com/authentication")
        logger.info(f"Authentication response status: {response.status_code}")

        if response.status_code != 201:
            raise Exception(f"Authentication failed: {response.text}")
        logger.info("Authentication successful")

    def load_submission_history(self):
        """Load submission history to track daily submissions."""
        if os.path.exists(self.提交日志文件):
            try:
                with open(self.提交日志文件, "r") as f:
                    data = json.load(f)
                    self.last_submission_date = data.get("last_submission_date")
                    logger.info(
                        f"Loaded submission history. Last submission: {self.last_submission_date}"
                    )
            except Exception as e:
                logger.warning(f"Could not load submission history: {e}")
                self.last_submission_date = None
        else:
            self.last_submission_date = None

    def save_submission_history(self):
        """Save submission history."""
        data = {
            "last_submission_date": self.last_submission_date,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self.提交日志文件, "w") as f:
            json.dump(data, f, indent=2)

    def start_vram_monitoring(self):
        """启动 VRAM 监控线程"""
        if self.vram_monitoring_active:
            logger.info("VRAM 监控已启动")
            return

        self.vram_monitoring_active = True
        self.vram_monitor_thread = threading.Thread(
            target=self._vram_monitor_loop, daemon=True
        )
        self.vram_monitor_thread.start()
        logger.info("已启动 VRAM 监控线程")

    def stop_vram_monitoring(self):
        """停止 VRAM 监控"""
        self.vram_monitoring_active = False
        if self.vram_monitor_thread:
            self.vram_monitor_thread.join(timeout=5)
        logger.info("已停止 VRAM 监控")

    def _vram_monitor_loop(self):
        """VRAM 监控循环，检查错误并处理模型降级"""
        logger.info("VRAM 监控循环已启动")

        while self.vram_monitoring_active and self.running:
            try:
                # 检查最近的日志中的 VRAM 错误
                if self._check_for_vram_errors():
                    logger.warning("在监控循环中检测到 VRAM 错误")
                    if self.model_fleet_manager.handle_vram_error():
                        logger.info("由于 VRAM 问题，已采取模型队列操作")
                        # 使用新模型配置重新启动 alpha 生成器
                        self._restart_alpha_generator()

                time.sleep(30)  # 每 30 秒检查一次

            except Exception as e:
                logger.error(f"VRAM 监控循环中出错: {e}")
                time.sleep(60)  # 出错时等待更长时间

        logger.info("VRAM 监控循环已停止")

    def _check_for_vram_errors(self) -> bool:
        """检查最近的日志中的 VRAM 错误"""
        try:
            # 检查 Ollama 日志和应用日志中的 VRAM 错误
            log_files_to_check = [
                "/app/logs/ollama.log",  # Ollama 日志重定向到文件
                "alpha_orchestrator.log",
                "alpha_generator_ollama.log",
            ]

            for log_file in log_files_to_check:
                try:
                    if os.path.exists(log_file):
                        # 读取日志文件的最后 50 行
                        with open(log_file, "r") as f:
                            lines = f.readlines()
                            recent_lines = lines[-50:] if len(lines) > 50 else lines

                            for line in recent_lines:
                                if self.model_fleet_manager.detect_vram_error(line):
                                    logger.warning(
                                        f"在 {log_file} 中发现 VRAM 错误: {line.strip()}"
                                    )
                                    return True
                except Exception as e:
                    # 跳过无法读取的文件
                    continue

            return False
        except Exception as e:
            logger.error(f"检查 VRAM 错误时出错: {e}")
            return False

    def _restart_alpha_generator(self):
        """使用新模型重新启动 alpha 生成器"""
        try:
            logger.info("正在使用新模型重新启动 alpha 生成器")

            # 如果当前生成器进程正在运行，则停止
            if self.generator_process and self.generator_process.poll() is None:
                self.generator_process.terminate()
                self.generator_process.wait(timeout=30)

            # 以连续模式启动新的生成器进程
            self.start_alpha_generator_continuous(batch_size=3, sleep_time=30)

        except Exception as e:
            logger.error(f"重新启动 alpha 生成器时出错: {e}")

    def start_restart_monitoring(self):
        """启动重启监控线程"""
        if not self.restart_thread or not self.restart_thread.is_alive():
            self.restart_thread = threading.Thread(
                target=self._restart_monitor_loop, daemon=True
            )
            self.restart_thread.start()
            logger.info("已启动重启监控 (30 分钟间隔)")

    def _restart_monitor_loop(self):
        """监控并每 30 分钟重启进程"""
        while self.running:
            try:
                current_time = time.time()
                time_since_last_restart = current_time - self.last_restart_time

                if time_since_last_restart >= self.restart_interval:
                    logger.info(
                        "距离上次重启已过去 30 分钟，正在启动重启..."
                    )
                    self.restart_all_processes()
                else:
                    remaining_time = self.restart_interval - time_since_last_restart
                    logger.debug(f"⏰ 下次重启将在 {remaining_time/60:.1f} 分钟后")

                # 每分钟检查一次
                time.sleep(60)

            except Exception as e:
                logger.error(f"重启监控中出错: {e}")
                time.sleep(60)

    def get_model_fleet_status(self) -> Dict:
        """获取模型队列的当前状态"""
        return self.model_fleet_manager.get_fleet_status()

    def reset_model_fleet(self):
        """将模型队列重置为最大的模型"""
        return self.model_fleet_manager.reset_to_largest_model()

    def force_model_downgrade(self):
        """强制降级到下一个更小的模型"""
        return self.model_fleet_manager.downgrade_model()

    def force_application_reset(self):
        """强制完整的应用重置"""
        logger.warning("正在强制应用重置")
        return self.model_fleet_manager.trigger_application_reset()

    def can_submit_today(self) -> bool:
        """检查今天是否可以提交 alpha（每天仅一次）"""
        today = datetime.now().date().isoformat()

        if self.last_submission_date == today:
            logger.info(f"今天已提交 ({today})。跳过提交。")
            return False

        logger.info(
            f"今天可以提交。上次提交时间: {self.last_submission_date}"
        )
        return True

    def run_alpha_expression_miner(
        self, promising_alpha_file: str = "hopeful_alphas.json"
    ):
        """对有潜力的 alpha 运行 alpha 表达式挖掘器"""
        logger.info("正在对有潜力的 alpha 启动 alpha 表达式挖掘器...")

        if not os.path.exists(promising_alpha_file):
            logger.warning(
                f"未找到有潜力的 alpha 文件 {promising_alpha_file}。跳过挖掘。"
            )
            return

        try:
            with open(promising_alpha_file, "r") as f:
                promising_alphas = json.load(f)

            if not promising_alphas:
                logger.info("未找到有潜力的 alpha。跳过挖掘。")
                return

            logger.info(f"找到 {len(promising_alphas)} 个有潜力的 alpha 用于挖掘")

            # 为每个有潜力的 alpha 运行 alpha 表达式挖掘器
            # 注意：挖掘器会自动从 hopeful_alphas.json 中移除成功挖掘的 alpha
            for i, alpha_data in enumerate(promising_alphas, 1):
                expression = alpha_data.get("expression", "")
                if not expression:
                    continue

                logger.info(
                    f"正在挖掘 alpha {i}/{len(promising_alphas)}: {expression[:100]}..."
                )

                # 以子进程运行 alpha 表达式挖掘器
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "alpha_expression_miner.py",
                            "--expression",
                            expression,
                            "--auto-mode",  # 以自动化模式运行
                            "--output-file",
                            f"mining_results_{i}.json",
                        ],
                        capture_output=True,
                        text=True,
                    )

                    if result.returncode == 0:
                        logger.info(f"Successfully mined alpha {i}")
                        # The alpha will be automatically removed from hopeful_alphas.json by the miner
                    else:
                        logger.error(f"Failed to mine alpha {i}: {result.stderr}")
                        # Failed alphas remain in hopeful_alphas.json for retry

                except subprocess.TimeoutExpired:
                    logger.error(f"Mining alpha {i} timed out")
                except Exception as e:
                    logger.error(f"Error mining alpha {i}: {e}")

                # Small delay between mining operations
                time.sleep(5)

        except Exception as e:
            logger.error(f"Error running alpha expression miner: {e}")

    def run_alpha_submitter(self, batch_size: int = 5):
        """运行 alpha 提交器，每日限速"""
        logger.info("正在启动 alpha 提交器...")

        if not self.can_submit_today():
            return

        try:
            # 优先使用带有凭证的改进提交器
            submitter_script = "improved_alpha_submitter.py"
            if not os.path.exists(submitter_script):
                logger.error(f"未找到提交器脚本: {submitter_script}")
                return

            result = subprocess.run(
                [
                    sys.executable,
                    submitter_script,
                    "--credentials",
                    self.credentials_path,
                    "--batch-size",
                    str(batch_size),
                    "--auto-mode",  # 单次运行
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )

            if result.returncode == 0:
                logger.info("alpha 提交成功完成")
                # 更新提交日期
                self.last_submission_date = datetime.now().date().isoformat()
                self.save_submission_history()
            else:
                logger.error(f"alpha 提交失败: {result.stderr}")

        except subprocess.TimeoutExpired:
            logger.error("alpha 提交超时")
        except Exception as e:
            logger.error(f"运行 alpha 提交器时出错: {e}")

    def run_alpha_generator(self, batch_size: int = 5, sleep_time: int = 30):
        """运行主 alpha 生成器（使用 Ollama）"""
        logger.info("正在启动 alpha 生成器（使用 Ollama）...")

        # 从模型队列管理器获取当前模型
        current_model = self.model_fleet_manager.get_current_model().name
        logger.info(f"使用的模型: {current_model}")

        try:
            # 以子进程运行 alpha 生成器
            result = subprocess.run(
                [
                    sys.executable,
                    "alpha_generator_ollama.py",
                    "--batch-size",
                    str(batch_size),
                    "--sleep-time",
                    str(sleep_time),
                    "--ollama-url",
                    self.ollama_url,
                    "--ollama-model",
                    current_model,
                    "--max-concurrent",
                    str(self.max_concurrent_simulations),
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )  # 1 小时超时

            if result.returncode == 0:
                logger.info("alpha 生成器成功完成")
            else:
                logger.error(f"alpha 生成器失败: {result.stderr}")

        except subprocess.TimeoutExpired:
            logger.error("alpha 生成器超时")
        except Exception as e:
            logger.error(f"运行 alpha 生成器时出错: {e}")

    def start_alpha_generator_continuous(
        self, batch_size: int = 3, sleep_time: int = 30
    ):
        """以连续模式启动 alpha 生成器（后台进程）"""
        logger.info("正在以连续模式启动 alpha 生成器...")

        # 从模型队列管理器获取当前模型
        current_model = self.model_fleet_manager.get_current_model().name
        logger.info(f"使用的模型: {current_model}")

        try:
            self.generator_process = subprocess.Popen(
                [
                    sys.executable,
                    "alpha_generator_ollama.py",
                    "--batch-size",
                    str(batch_size),
                    "--sleep-time",
                    str(sleep_time),
                    "--ollama-url",
                    self.ollama_url,
                    "--ollama-model",
                    current_model,
                    "--max-concurrent",
                    str(self.max_concurrent_simulations),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            logger.info(
                f"alpha 生成器已启动，PID: {self.generator_process.pid}"
            )

        except Exception as e:
            logger.error(f"Error starting alpha generator: {e}")

    def start_alpha_expression_miner_continuous(self, check_interval: int = 300):
        """以连续模式启动 alpha 表达式挖掘器"""
        logger.info("正在以连续模式启动 alpha 表达式挖掘器...")

        while self.running:
            try:
                # 检查 hopeful_alphas.json 是否存在且有内容
                if os.path.exists("hopeful_alphas.json"):
                    try:
                        with open("hopeful_alphas.json", "r") as f:
                            alphas = json.load(f)
                            if alphas and len(alphas) > 0:
                                logger.info(f"找到 {len(alphas)} 个 alpha 用于挖掘")
                                self.run_alpha_expression_miner()
                            else:
                                logger.info("未在 hopeful_alphas.json 中找到 alpha")
                    except json.JSONDecodeError:
                        logger.warning(
                            "hopeful_alphas.json 不是有效的 JSON，等待有效数据..."
                        )
                    except Exception as e:
                        logger.error(f"读取 hopeful_alphas.json 时出错: {e}")
                else:
                    logger.info(
                        "尚未找到 hopeful_alphas.json，等待 alpha 生成器创建有潜力的 alpha..."
                    )

                # 等待下一次检查
                time.sleep(check_interval)

            except Exception as e:
                logger.error(f"连续挖掘器中出错: {e}")
                time.sleep(check_interval)

    def restart_all_processes(self):
        """重启所有运行中的进程以防止任务卡住"""
        logger.info("正在重启所有进程以防止任务卡住...")

        # 停止当前进程
        self.stop_processes()

        # 等待进程终止
        time.sleep(5)

        # 重启进程
        try:
            # 重启 alpha 生成器
            logger.info("正在重启 alpha 生成器...")
            self.start_alpha_generator_continuous(batch_size=3, sleep_time=30)

            # 重启 VRAM 监控
            logger.info("正在重启 VRAM 监控...")
            self.start_vram_monitoring()

            logger.info("所有进程已成功重启")
            self.last_restart_time = time.time()

        except Exception as e:
            logger.error(f"❌ Error during restart: {e}")

    def stop_processes(self):
        """停止所有运行中的进程"""
        logger.info("正在停止所有进程...")
        self.running = False

        # 停止重启线程
        if self.restart_thread and self.restart_thread.is_alive():
            logger.info("正在停止重启监控线程...")

        # 停止 VRAM 监控
        self.stop_vram_monitoring()

        if self.generator_process:
            logger.info("正在终止 alpha 生成器进程...")
            self.generator_process.terminate()
            try:
                self.generator_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                logger.warning("正在强制终止 alpha 生成器进程...")
                self.generator_process.kill()

        if self.miner_process:
            logger.info("正在终止 alpha 挖掘器进程...")
            self.miner_process.terminate()
            try:
                self.miner_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                logger.warning("正在强制终止 alpha 挖掘器进程...")
                self.miner_process.kill()

    def daily_workflow(self):
        """运行完整的每日工作流"""
        logger.info("正在启动每日 alpha 工作流...")

        # 1. 运行 alpha 生成器几小时
        logger.info("阶段 1: 正在运行 alpha 生成器...")
        self.run_alpha_generator(batch_size=3, sleep_time=60)

        # 2. 对有潜力的 alpha 运行 alpha 表达式挖掘器
        logger.info("阶段 2: 正在运行 alpha 表达式挖掘器...")
        self.run_alpha_expression_miner()

        # 3. 运行 alpha 提交器（每天一次）
        logger.info("阶段 3: 正在运行 alpha 提交器...")
        self.run_alpha_submitter(batch_size=3)

        logger.info("每日工作流已完成")

    def continuous_mining(self, mining_interval_hours: int = 6):
        """运行连续挖掘，并发 alpha 生成和表达式挖掘"""
        logger.info(
            f"正在启动连续挖掘，间隔 {mining_interval_hours} 小时..."
        )

        try:
            # 启动 VRAM 监控
            logger.info("正在启动 VRAM 监控...")
            self.start_vram_monitoring()

            # 启动重启监控
            logger.info("正在启动重启监控...")
            self.start_restart_monitoring()

            # 以连续模式启动 alpha 生成器
            self.start_alpha_generator_continuous(batch_size=3, sleep_time=30)

            # 在单独的线程中启动 alpha 表达式挖掘器
            miner_thread = threading.Thread(
                target=self.start_alpha_expression_miner_continuous,
                args=(mining_interval_hours * 3600,),  # 将小时转换为秒
                daemon=True,
            )
            miner_thread.start()

            # 每天下午 2 点安排提交
            schedule.every().day.at("14:00").do(self.run_alpha_submitter)

            logger.info(
                "alpha 生成器和表达式挖掘器正在并发运行"
            )
            logger.info(
                f"最大并发模拟数: {self.max_concurrent_simulations}"
            )

            while self.running:
                try:
                    # 运行待处理的任务
                    schedule.run_pending()

                    # 检查生成器进程是否仍在运行
                    if (
                        self.generator_process
                        and self.generator_process.poll() is not None
                    ):
                        logger.warning("alpha 生成器进程已停止，正在重启...")
                        self.start_alpha_generator_continuous(
                            batch_size=3, sleep_time=30
                        )

                    # 下次循环前的小延迟
                    time.sleep(1860)

                except KeyboardInterrupt:
                    logger.info("收到中断信号，正在停止...")
                    break
                except Exception as e:
                    logger.error(f"连续挖掘中出错: {e}")
                    time.sleep(300)  # 重试前等待 5 分钟

        finally:
            self.stop_processes()


def main():
    parser = argparse.ArgumentParser(
        description="Alpha 编排器 - 管理 alpha 生成、挖掘和提交"
    )
    parser.add_argument(
        "--credentials",
        type=str,
        default="./credential.txt",
        help="凭证文件路径 (默认: ./credential.txt)",
    )
    parser.add_argument(
        "--ollama-url",
        type=str,
        default="http://localhost:11434",
        help="Ollama API URL (默认: http://localhost:11434)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=[
            "daily",
            "continuous",
            "miner",
            "submitter",
            "generator",
            "fleet-status",
            "fleet-reset",
            "fleet-downgrade",
            "fleet-reset-app",
            "restart",
        ],
        default="continuous",
        help="操作模式 (默认: continuous)",
    )
    parser.add_argument(
        "--mining-interval",
        type=int,
        default=6,
        help="连续模式下的挖掘间隔（小时） (默认: 6)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="操作批次大小 (默认: 3)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="最大并发模拟数 (默认: 3)",
    )
    parser.add_argument(
        "--restart-interval",
        type=int,
        default=30,
        help="重启间隔（分钟） (默认: 30)",
    )
    parser.add_argument(
        "--ollama-model",
        type=str,
        default="deepseek-r1:8b",
        help="使用的 Ollama 模型 (默认: deepseek-r1:8b)",
    )

    args = parser.parse_args()

    try:
        orchestrator = AlphaOrchestrator(args.credentials, args.ollama_url)
        orchestrator.max_concurrent_simulations = args.max_concurrent
        orchestrator.restart_interval = (
            args.restart_interval * 60
        )  # 将分钟转换为秒

        # 更新模型队列以使用指定的模型
##        if args.ollama_model:
##            # 在队列中找到模型并设置为当前模型
##            for i, model_info in enumerate(
##                orchestrator.model_fleet_manager.model_fleet
##            ):
##                if model_info.name == args.ollama_model:
##                    orchestrator.model_fleet_manager.current_model_index = i
##                    orchestrator.model_fleet_manager.save_state()
##                    logger.info(f"设置模型队列使用: {args.ollama_model}")
##                    break

        if args.mode == "daily":
            orchestrator.daily_workflow()
        elif args.mode == "continuous":
            orchestrator.continuous_mining(args.mining_interval)
        elif args.mode == "miner":
            orchestrator.run_alpha_expression_miner()
        elif args.mode == "submitter":
            orchestrator.run_alpha_submitter(args.batch_size)
        elif args.mode == "generator":
            orchestrator.run_alpha_generator(args.batch_size)
        elif args.mode == "fleet-status":
            status = orchestrator.get_model_fleet_status()
            print(json.dumps(status, indent=2))
        elif args.mode == "fleet-reset":
            orchestrator.reset_model_fleet()
            print("模型队列已重置为首选模型 (索引 0)")
        elif args.mode == "fleet-downgrade":
            orchestrator.force_model_downgrade()
            print("模型队列已降级到下一个更小的模型")
        elif args.mode == "fleet-reset-app":
            orchestrator.force_application_reset()
            print("应用重置完成 - 已固定到首选模型")
        elif args.mode == "restart":
            orchestrator.restart_all_processes()
            print("手动重启完成")

    except Exception as e:
        logger.error(f"致命错误: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
