#!/bin/bash
set -e

echo "🐲 隆小侠 LONG CLAW — 云端启动"
echo "  时区: $(date +'%Z %Y-%m-%d %H:%M:%S')"
echo "  Futu: ${FUTU_HOST:-127.0.0.1}:${FUTU_PORT:-11111}"
echo "  模式: ${DEPLOY_MODE:-local}"

# cron 模式: 启动定时任务守护进程
if [ "$1" = "cron" ]; then
    echo "  启动定时任务模式..."
    # 将环境变量注入 cron 环境
    printenv | grep -E '^(FUTU_|FEISHU_|DEPLOY_|TZ|PATH|PYTHONPATH)' \
        > /etc/environment
    crontab /etc/cron.d/signals-cron
    echo "  定时任务已加载:"
    crontab -l | grep -v '^#' | grep -v '^$'
    exec cron -f
fi

# 默认: 直接执行传入的命令
exec "$@"
