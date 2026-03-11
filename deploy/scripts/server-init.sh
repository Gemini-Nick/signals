#!/bin/bash
# 隆小侠 LONG CLAW — 中国云服务器一键初始化
# 用法: ssh root@<IP> 'bash -s' < deploy/scripts/server-init.sh
set -e

echo "🐲 隆小侠服务器初始化..."

# 1. 系统更新
echo ">>> 系统更新..."
apt-get update && apt-get upgrade -y

# 2. 安装 Docker
echo ">>> 安装 Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker && systemctl start docker
else
    echo "  Docker 已安装: $(docker --version)"
fi

# 3. 安装 Docker Compose
echo ">>> 安装 Docker Compose..."
apt-get install -y docker-compose-plugin git

# 4. 配置时区
echo ">>> 设置时区: Asia/Shanghai"
timedatectl set-timezone Asia/Shanghai

# 5. 创建工作目录
echo ">>> 创建工作目录 /opt/signals..."
mkdir -p /opt/signals
cd /opt/signals

# 6. 克隆代码 (如果尚未克隆)
if [ ! -d ".git" ]; then
    echo ">>> 请手动克隆代码:"
    echo "    cd /opt/signals"
    echo "    git clone https://github.com/<YOUR_USER>/signals.git ."
else
    echo "  代码仓库已存在"
fi

echo ""
echo "✅ 初始化完成！后续步骤:"
echo "  1. cd /opt/signals"
echo "  2. git clone https://github.com/<YOUR_USER>/signals.git . (如果尚未克隆)"
echo "  3. cp deploy/.env.example deploy/.env"
echo "  4. vim deploy/.env  # 填写 Futu 和飞书凭证"
echo "  5. cd deploy && docker compose up -d"
echo "  6. docker compose logs -f signals  # 查看日志"
