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
    sync)
        if [ -f logs/sync.pid ]; then
            kill $(cat logs/sync.pid) 2>/dev/null && echo "  Sync Worker 已停止"
            rm -f logs/sync.pid
        else
            echo "  Sync Worker 未在运行"
        fi
        ;;
    mongo)
        if command -v docker &>/dev/null; then
            cd "$WORK/deploy" 2>/dev/null
            docker compose stop mongo 2>/dev/null || docker-compose stop mongo 2>/dev/null || true
            cd "$WORK" 2>/dev/null
            echo "  MongoDB 已停止"
        else
            echo "  Docker 未安装"
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
        if [ -f logs/sync.pid ]; then
            kill $(cat logs/sync.pid) 2>/dev/null && echo "  Sync Worker 已停止"
            rm -f logs/sync.pid
        fi
        if [ -f logs/futu.pid ]; then
            kill $(cat logs/futu.pid) 2>/dev/null && echo "  Futu OpenD 已停止"
            rm -f logs/futu.pid
        fi
        pkill -f FutuOpenD 2>/dev/null || true
        if command -v docker &>/dev/null; then
            cd "$WORK/deploy" 2>/dev/null
            docker compose stop mongo 2>/dev/null || docker-compose stop mongo 2>/dev/null || true
            cd "$WORK" 2>/dev/null
            echo "  MongoDB 已停止"
        fi
        echo "✅ 全部已停止"
        ;;
    *)
        echo "用法: $0 [web|web2|sync|mongo|all]"
        exit 1
        ;;
esac
