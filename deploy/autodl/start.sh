#!/bin/bash
# 隆小侠 LONG CLAW — AutoDL 启动服务
#
# 用法:
#   bash deploy/autodl/start.sh              # 全部启动 (web=6006, web2=6008)
#   bash deploy/autodl/start.sh web 6006     # 只启动 web，端口 6006
#   bash deploy/autodl/start.sh web2 6008    # 只启动 web2，端口 6008
#   bash deploy/autodl/start.sh all          # 全部启动（默认端口）
set -e

WORK=${SIGNALS_WORK:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$WORK"

# 加载环境变量
set -a
source deploy/.env 2>/dev/null || true
set +a
export DEPLOY_MODE=cloud
export TZ=Asia/Shanghai

echo "🐲 隆小侠 AutoDL 启动..."
echo "  时区: $(date +'%Z %Y-%m-%d %H:%M:%S')"

# ── 启动函数 ──────────────────────────────────────
start_futu() {
    FUTU_DIR=/root/autodl-tmp/futu
    if [ -f "$FUTU_DIR/FutuOpenD" ]; then
        pkill -f FutuOpenD 2>/dev/null || true
        sleep 1
        cd "$FUTU_DIR"
        nohup ./FutuOpenD \
            -login_account "$FUTU_ACCOUNT" \
            -login_pwd_md5 "$FUTU_PWD_MD5" \
            -lang chs \
            > "$WORK/logs/futu.log" 2>&1 &
        echo $! > "$WORK/logs/futu.pid"
        echo "  🔌 Futu OpenD: PID=$(cat $WORK/logs/futu.pid), port=11111"
        cd "$WORK"
        echo -n "  等待 Futu 就绪"
        for i in $(seq 1 15); do
            if nc -z 127.0.0.1 11111 2>/dev/null; then
                echo " ✅"; break
            fi
            echo -n "."; sleep 2
        done
        nc -z 127.0.0.1 11111 2>/dev/null || echo " ⚠️ Futu 未就绪，继续启动..."
    else
        echo "  ⚠️ Futu OpenD 未安装，跳过（A股数据用 AKShare/东财）"
    fi
}

start_web() {
    local port=${1:-6006}
    if [ -f logs/web.pid ]; then
        kill $(cat logs/web.pid) 2>/dev/null || true
        rm -f logs/web.pid
    fi
    nohup python run.py --mode web --port "$port" > logs/web.log 2>&1 &
    echo $! > logs/web.pid
    echo "  🌐 Web  服务: PID=$(cat logs/web.pid), port=$port"
}

start_web2() {
    local port=${1:-6008}
    if [ -f logs/web2.pid ]; then
        kill $(cat logs/web2.pid) 2>/dev/null || true
        rm -f logs/web2.pid
    fi
    nohup python run.py --mode web2 --port "$port" > logs/web2.log 2>&1 &
    echo $! > logs/web2.pid
    echo "  🐲 Web2 服务: PID=$(cat logs/web2.pid), port=$port"
}

# ── 参数解析 ──────────────────────────────────────
mkdir -p logs
TARGET=${1:-all}
PORT=$2

case "$TARGET" in
    web)
        start_futu
        start_web "${PORT:-6006}"
        echo -e "\n✅ Web 已启动！日志: tail -f $WORK/logs/web.log"
        ;;
    web2)
        start_web2 "${PORT:-6008}"
        echo -e "\n✅ Web2 已启动！日志: tail -f $WORK/logs/web2.log"
        ;;
    all)
        start_futu
        start_web 6006
        start_web2 6008
        echo ""
        echo "✅ 全部启动！"
        echo "  Web  日志: tail -f $WORK/logs/web.log"
        echo "  Web2 日志: tail -f $WORK/logs/web2.log"
        echo "  Futu 日志: tail -f $WORK/logs/futu.log"
        echo "  访问: AutoDL → 自定义服务 (6006=Web, 6008=Web2)"
        ;;
    *)
        echo "用法: $0 [web|web2|all] [端口号]"
        echo "  $0              → 全部启动 (web=6006, web2=6008)"
        echo "  $0 web 6006     → 只启动 web"
        echo "  $0 web2 6008    → 只启动 web2"
        exit 1
        ;;
esac
