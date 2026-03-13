#!/bin/bash
# 隆小侠 LONG CLAW — AutoDL 停止服务
# 用法: bash deploy/autodl/stop.sh

WORK=${SIGNALS_WORK:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$WORK" 2>/dev/null || true

echo "🐲 停止隆小侠服务..."

# 停止 Web
if [ -f logs/web.pid ]; then
    kill $(cat logs/web.pid) 2>/dev/null && echo "  Web 已停止"
    rm -f logs/web.pid
fi

# 停止 Web2
if [ -f logs/web2.pid ]; then
    kill $(cat logs/web2.pid) 2>/dev/null && echo "  Web2 已停止"
    rm -f logs/web2.pid
fi

# 停止 Futu OpenD
if [ -f logs/futu.pid ]; then
    kill $(cat logs/futu.pid) 2>/dev/null && echo "  Futu OpenD 已停止"
    rm -f logs/futu.pid
fi
pkill -f FutuOpenD 2>/dev/null || true

echo "✅ 全部已停止"
