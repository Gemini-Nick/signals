#!/bin/bash
# 隆小侠 LONG CLAW — AutoDL 一键初始化
# 用法: bash deploy/autodl/setup.sh
set -e

WORK=${SIGNALS_WORK:-$(cd "$(dirname "$0")/../.." && pwd)}
REPO=${GITHUB_REPO:-"https://github.com/Gemini-Nick/signals.git"}

echo "🐲 隆小侠 AutoDL 部署初始化..."
echo "  工作目录: $WORK"

# ── 1. 克隆代码 ──────────────────────────────────
if [ -d "$WORK/.git" ]; then
    echo "  代码仓库已存在，拉取最新..."
    cd "$WORK"
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
    git pull origin "$BRANCH"
else
    echo ">>> 克隆代码..."
    git clone "$REPO" "$WORK"
    cd "$WORK"
fi

# ── 2. Python 依赖 ──────────────────────────────
echo ">>> 初始化 Python 3.11 环境..."
bash scripts/bootstrap-dev.sh

# ── 3. Futu OpenD ──────────────────────────────
FUTU_DIR=${FUTU_DIR:-/root/futu}
if [ ! -f "$FUTU_DIR/FutuOpenD" ]; then
    mkdir -p "$FUTU_DIR"
    echo ""
    echo "⚠️  Futu OpenD 尚未安装"
    echo "    1. 下载 Linux 版: https://www.futunn.com/download/openAPI"
    echo "    2. 解压到: $FUTU_DIR/"
    echo "    3. 确保可执行: chmod +x $FUTU_DIR/FutuOpenD"
    echo ""
else
    echo "  Futu OpenD 已安装: $FUTU_DIR/FutuOpenD"
fi

# ── 4. 环境变量 ──────────────────────────────────
if [ ! -f deploy/.env ]; then
    cp deploy/.env.example deploy/.env
    echo ""
    echo ">>> 请编辑凭证:"
    echo "    vim $WORK/deploy/.env"
    echo ""
else
    echo "  deploy/.env 已存在"
fi

# ── 5. 东财缓存检查 ──────────────────────────────
if [ -f .cache/name_to_code.json ] && [ -f .cache/board_industry.csv ]; then
    echo "  东财缓存已存在（随 git 同步）"
else
    echo ">>> 东财缓存缺失，尝试生成..."
    bash scripts/python.sh deploy/autodl/gen_cache.py || echo "  ⚠️  缓存生成失败（云端东财不通属正常）"
    echo "  💡 可在本地运行: bash scripts/python.sh deploy/autodl/gen_cache.py --push"
fi

# ── 6. cron 定时任务 ──────────────────────────────
echo ">>> 配置定时任务..."
CRON_FILE=/tmp/signals-cron
sed "s|/app|$WORK|g" deploy/cron/signals-cron > "$CRON_FILE"
# 添加自动代码同步 (每小时)
echo "" >> "$CRON_FILE"
echo "# ── AutoDL 自动同步 ──────────────────────────" >> "$CRON_FILE"
echo "0 * * * *  cd $WORK && bash deploy/autodl/sync.sh >> logs/sync.log 2>&1" >> "$CRON_FILE"
crontab "$CRON_FILE"
echo "  定时任务已安装 (crontab -l 查看)"

# ── 7. 数据 & 日志目录 ────────────────────────────
mkdir -p .cache .data logs

echo ""
echo "✅ 初始化完成！后续步骤:"
echo "  1. vim deploy/.env           # 填写 Futu 和飞书凭证"
echo "  2. bash deploy/autodl/start.sh  # 启动服务"
echo "  3. AutoDL 控制台 → 自定义服务 → 访问 Web UI"
