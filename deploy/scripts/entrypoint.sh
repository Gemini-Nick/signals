#!/bin/bash
set -e

echo "🐲 隆小侠 LONG CLAW — 云端启动"
echo "  时区: $(date +'%Z %Y-%m-%d %H:%M:%S')"
echo "  Futu: ${FUTU_HOST:-127.0.0.1}:${FUTU_PORT:-11111}"
echo "  模式: ${DEPLOY_MODE:-local}"

# 将环境变量注入 cron 环境
printenv | grep -E '^(FUTU_|FEISHU_|DEPLOY_|TZ|PATH|PYTHONPATH)' \
    > /etc/environment

# 启动 cron 后台定时任务
if [ -f /etc/cron.d/signals-cron ]; then
    crontab /etc/cron.d/signals-cron
    cron
    echo "  定时任务已加载:"
    crontab -l | grep -v '^#' | grep -v '^$'
fi

# 前台启动 Web 服务
echo "  启动 Web 预览服务 (port 8000)..."
exec python run.py --mode web --port 8000
