#!/bin/bash
# 隆小侠 LONG CLAW — AutoDL 启动服务
# 用法: bash deploy/autodl/start.sh
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

# ── Futu OpenD ──────────────────────────────────
FUTU_DIR=/root/autodl-tmp/futu
if [ -f "$FUTU_DIR/FutuOpenD" ]; then
    # 杀旧 Futu 进程
    pkill -f FutuOpenD 2>/dev/null || true
    sleep 1

    # 启动 Futu OpenD (后台)
    cd "$FUTU_DIR"
    nohup ./FutuOpenD \
        -login_account "$FUTU_ACCOUNT" \
        -login_pwd_md5 "$FUTU_PWD_MD5" \
        -lang chs \
        > "$WORK/logs/futu.log" 2>&1 &
    echo $! > "$WORK/logs/futu.pid"
    echo "  🔌 Futu OpenD: PID=$(cat $WORK/logs/futu.pid), port=11111"
    cd "$WORK"

    # 等待 Futu 就绪 (最多 30 秒)
    echo -n "  等待 Futu 就绪"
    for i in $(seq 1 15); do
        if nc -z 127.0.0.1 11111 2>/dev/null; then
            echo " ✅"
            break
        fi
        echo -n "."
        sleep 2
    done
    nc -z 127.0.0.1 11111 2>/dev/null || echo " ⚠️ Futu 未就绪，继续启动..."
else
    echo "  ⚠️ Futu OpenD 未安装，跳过（A股数据用 AKShare/东财）"
fi

# ── Web 服务 ──────────────────────────────────────
# 杀旧 Web 进程
if [ -f logs/web.pid ]; then
    kill $(cat logs/web.pid) 2>/dev/null || true
    rm -f logs/web.pid
fi

# 启动 Web (端口 6006 = AutoDL 默认映射公网端口)
nohup python run.py --mode web --port 6006 > logs/web.log 2>&1 &
echo $! > logs/web.pid
echo "  🌐 Web 服务: PID=$(cat logs/web.pid), port=6006"
echo ""
echo "✅ 启动完成！"
echo "  Web 日志: tail -f $WORK/logs/web.log"
echo "  Futu 日志: tail -f $WORK/logs/futu.log"
echo "  访问: AutoDL 控制台 → 自定义服务"
