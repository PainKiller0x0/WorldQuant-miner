# === 使用这份简化版的 alpha_orchestrator.py 替换你的旧文件 ===
import argparse
import json
import os
import time
import logging
import subprocess
import sys
from requests.auth import HTTPBasicAuth
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("alpha_orchestrator.log")],
)
logger = logging.getLogger(__name__)

class AlphaOrchestrator:
    def __init__(self, credentials_path: str):
        self.credentials_path = credentials_path
        with open(credentials_path) as f:
            credentials = json.load(f)
        self.wq_user_id, self.wq_api_key = credentials
        
        self.running = True
        self.generator_process = None
        self.setup_auth()

    def setup_auth(self):
        logger.info("Authenticating with WorldQuant Brain...")
        sess = requests.Session()
        sess.auth = HTTPBasicAuth(self.wq_user_id, self.wq_api_key)
        response = sess.post("https://api.worldquantbrain.com/authentication")
        if response.status_code != 201:
            raise Exception(f"WQ Authentication failed: {response.text}")
        logger.info("WQ Authentication successful")

    def start_alpha_generator(self, batch_size: int = 2):
        logger.info("正在以连续模式启动 alpha 生成器 (ClawCloud API)...")
        try:
            self.generator_process = subprocess.Popen(
                [
                    sys.executable,
                    "alpha_generator_ollama.py",
                    "--user-id", self.wq_user_id,
                    "--api-key", self.wq_api_key,
                    "--batch-size", str(batch_size),
                ],
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True,
            )
            logger.info(f"alpha 生成器已启动，PID: {self.generator_process.pid}")
        except Exception as e:
            logger.error(f"启动 alpha 生成器失败: {e}")

    def continuous_mode(self, batch_size: int = 2):
        logger.info("正在启动连续挖掘...")
        self.start_alpha_generator(batch_size)

        while self.running:
            if self.generator_process and self.generator_process.poll() is not None:
                logger.warning("alpha 生成器进程已停止，正在重启...")
                time.sleep(10) # 等待10秒再重启
                self.start_alpha_generator(batch_size)
            
            time.sleep(60) # 主控每分钟检查一次子进程状态

    def stop_processes(self):
        logger.info("正在停止所有进程...")
        self.running = False
        if self.generator_process and self.generator_process.poll() is None:
            self.generator_process.terminate()
            try:
                self.generator_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.generator_process.kill()

def main():
    parser = argparse.ArgumentParser(description="Alpha Orchestrator (API Mode)")
    parser.add_argument("--credentials", default="./credential.txt")
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--mode", default="continuous")
    args = parser.parse_args()

    orchestrator = None
    try:
        orchestrator = AlphaOrchestrator(args.credentials)
        if args.mode == "continuous":
            orchestrator.continuous_mode(args.batch_size)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    except Exception as e:
        logger.critical(f"致命错误: {e}")
    finally:
        if orchestrator:
            orchestrator.stop_processes()

if __name__ == "__main__":
    main()