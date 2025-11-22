FROM python:3.10-slim

WORKDIR /app

# --- 核心修改开始 ---
# 1. 先只拷贝 requirements.txt
# 这样只要 requirements.txt 没变，这一层缓存就在
COPY requirements.txt /app/

# 2. 安装依赖 (这一步会生成缓存层)
# 只要 requirements.txt 不变，这一步永远不会重新跑，直接用缓存！
# 增加 --default-timeout 防止网络波动导致 PyTorch 下载中断
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=1000 --no-cache-dir -r requirements.txt

# 3. 依赖装好后，再拷贝剩下的代码
# 此时修改代码只会让这一行及之后的指令重新执行，而不会触发上面的 pip install
COPY . /app/
# --- 核心修改结束 ---

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

CMD ["python", "-u", "alpha_orchestrator.py"]