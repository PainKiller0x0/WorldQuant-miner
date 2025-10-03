# === 最终完整版，替换 alpha_orchestrator.py 的所有内容 (增强凭证读取) ===
import logging
import subprocess
import time
import argparse
import sys
import requests

# 日志配置
logging.basicConfig(level=logging.INFO,
                      format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                      handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

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
            response = self.session.post(url, auth=(self.user_id, self.api_key), timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"WorldQuant Brain authentication failed: {e}")
            raise

class AlphaOrchestrator:
    def __init__(self, credentials_file, batch_size, mode):
        self.process = None
        self.credentials_file = credentials_file
        self.batch_size = batch_size
        self.mode = mode
        # <--- [核心修改] 将加载凭证的调用移到主逻辑中 ---
        self.wq_user_id = None
        self.wq_api_key = None
        self.wq = None

    def load_credentials(self, file_path):
        """更健壮的凭证加载函数"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
            
            if len(lines) < 2:
                logger.critical(f"错误: {file_path} 文件内容不足两行。")
                return False

            creds = {}
            for line in lines:
                if ':' not in line:
                    logger.critical(f"错误: {file_path} 中的行 '{line}' 缺少 ':' 分隔符。")
                    return False
                key, value = line.split(':', 1)
                creds[key.strip()] = value.strip()
            
            if 'user_id' not in creds or 'api_key' not in creds:
                logger.critical(f"错误: {file_path} 必须包含 'user_id' 和 'api_key' 两项。")
                return False

            self.wq_user_id = creds['user_id']
            self.wq_api_key = creds['api_key']
            logger.info("成功从 credential.txt 加载凭证。")
            return True

        except FileNotFoundError:
            logger.critical(f"错误: 凭证文件 {file_path} 未找到。")
            return False
        except Exception as e:
            logger.critical(f"从 {file_path} 加载凭证时出现未知错误: {e}")
            return False

    def connect_wq(self):
        try:
            logger.info("正在向 WorldQuant Brain 进行身份验证...")
            self.wq = WorldQuant(user_id=self.wq_user_id, api_key=self.wq_api_key)
            logger.info("WQ 身份验证成功")
        except Exception as e:
            logger.critical(f"无法向 WorldQuant 进行身份验证: {e}")
            self.wq = None

    def start_generator(self, api_config_path):
        if self.process and self.process.poll() is None:
            logger.info("Alpha 生成器已在运行。")
            return
        
        command = [
            'python', '-u', 'alpha_generator_ollama.py',
            '--user-id', self.wq_user_id,
            '--api-key', self.wq_api_key,
            '--batch-size', str(self.batch_size),
            '--api-config-path', api_config_path
        ]
        
        try:
            logger.info(f"正在以连续模式启动 alpha 生成器...")
            self.process = subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr)
            logger.info(f"alpha 生成器已启动，PID: {self.process.pid}")
        except Exception as e:
            logger.error(f"启动 alpha 生成器失败: {e}")
            self.process = None

    def monitor_and_restart(self, api_config_path):
        while True:
            if self.process is None or self.process.poll() is not None:
                if self.process and self.process.poll() is not None:
                    logger.warning(f"PID为 {self.process.pid} 的Alpha生成器进程已终止，代码: {self.process.poll()}. 正在重启...")
                else:
                    logger.warning("未找到Alpha生成器进程。正在启动...")
                self.start_generator(api_config_path)
            time.sleep(60)

def main():
    parser = argparse.ArgumentParser(description="Alpha 表达式挖掘编排器")
    parser.add_argument('--credentials', type=str, required=True, help='凭证文件路径。')
    parser.add_argument('--batch-size', type=int, default=10, help='每批次生成的alpha数量。')
    parser.add_argument('--mode', type=str, choices=['continuous', 'one-off'], default='continuous', help='运行模式。')
    args = parser.parse_args()
    
    orchestrator = AlphaOrchestrator(args.credentials, args.batch_size, args.mode)
    
    if not orchestrator.load_credentials(args.credentials):
        sys.exit(1) # 如果凭证加载失败，直接退出
        
    orchestrator.connect_wq()

    if orchestrator.wq is None:
        logger.critical("无法连接到 WorldQuant。正在退出。")
        return

    if args.mode == 'continuous':
        logger.info("正在启动连续挖掘...")
        orchestrator.start_generator('api_config.json')
        try:
            orchestrator.monitor_and_restart('api_config.json')
        except KeyboardInterrupt:
            logger.info("编排器被用户停止。")
            if orchestrator.process:
                orchestrator.process.terminate()
    else: # one-off
        orchestrator.start_generator('api_config.json')
        if orchestrator.process:
            orchestrator.process.wait()

if __name__ == "__main__":
    main()