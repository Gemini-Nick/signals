#!/bin/bash
# 隆小侠 LONG CLAW — AutoDL 停止服务
#
# 用法:
#   bash deploy/autodl/stop.sh          # 全部停止
#   bash deploy/autodl/stop.sh web      # 只停 web
#   bash deploy/autodl/stop.sh web2     # 只停 web2

WORK=${SIGNALS_WORK:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$WORK" 2>/dev/null || true

TARGET=${1:-all}

echo "🐲 停止隆小侠服务..."

case "$TARGET" in
    web)
        if [ -f logs/web.pid ]; then
            kill $(cat logs/web.pid) 2>/dev/null && echo "  Web 已停止"
            rm -f logs/web.pid
        else
            echo "  Web 未在运行"
        fi
        ;;
    web2)
        if [ -f logs/web2.pid ]; then
            kill $(cat logs/web2.pid) 2>/dev/null && echo "  Web2 已停止"
            rm -f logs/web2.pid
        else
            echo "  Web2 未在运行"
        fi
        ;;
    all)
        if [ -f logs/web.pid ]; then
            kill $(cat logs/web.pid) 2>/dev/null && echo "  Web 已停止"
            rm -f logs/web.pid
        fi
        if [ -f logs/web2.pid ]; then
            kill $(cat logs/web2.pid) 2>/dev/null && echo "  Web2 已停止"
            rm -f logs/web2.pid
        fi
        if [ -f logs/futu.pid ]; then
            kill $(cat logs/futu.pid) 2>/dev/null && echo "  Futu OpenD 已停止"
            rm -f logs/futu.pid
        fi
        pkill -f FutuOpenD 2>/dev/null || true
        echo "✅ 全部已停止"
        ;;
    *)
        echo "用法: $0 [web|web2|all]"
        exit 1
        ;;
esac
