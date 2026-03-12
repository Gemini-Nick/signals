#!/bin/bash
# 隆小侠 LONG CLAW — GitHub 代码同步 + 热重启
# 用法: bash deploy/autodl/sync.sh
# 可由 cron 自动调用，或 GitHub Actions SSH 触发

WORK=/root/autodl-tmp/signals
cd "$WORK"

echo "$(date +'%Y-%m-%d %H:%M:%S') 开始同步..."

# 拉取最新代码
OLD_HEAD=$(git rev-parse HEAD)
git pull origin main 2>&1
NEW_HEAD=$(git rev-parse HEAD)

# 无更新则跳过
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    echo "  无更新，跳过重启"
    exit 0
fi

echo "  更新: ${OLD_HEAD:0:7} → ${NEW_HEAD:0:7}"
git log --oneline "$OLD_HEAD".."$NEW_HEAD"

# 检查依赖是否变化
if git diff "$OLD_HEAD".."$NEW_HEAD" --name-only | grep -q requirements.txt; then
    echo "  依赖变化，重新安装..."
    pip install -r requirements.txt
fi

# 热重启 Web 服务
echo "  重启服务..."
bash deploy/autodl/start.sh

echo "$(date +'%Y-%m-%d %H:%M:%S') 同步完成 ✅"
