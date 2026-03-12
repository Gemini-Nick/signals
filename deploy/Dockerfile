FROM python:3.11-slim

WORKDIR /app

# 系统依赖 (czsc 需要 gcc 编译, cron 用于定时任务)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ cron netcat-openbsd && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 数据 & 日志目录
RUN mkdir -p /app/.data /app/logs

# 入口脚本
COPY deploy/scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
