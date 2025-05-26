# 使用 Python 基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 复制当前目录下的所有文件到工作目录
COPY . .

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    procps \
    gcc \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 升级 pip
RUN pip install --no-cache-dir --upgrade pip

# 安装 Python 依赖
RUN pip install --no-cache-dir flask==2.3.3 psutil==5.9.5 pytz

# 暴露 Flask 应用运行的端口
EXPOSE 36336

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 运行 Python 脚本
CMD ["python", "app.py"]