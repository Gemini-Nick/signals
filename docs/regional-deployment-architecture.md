# 云端部署架构设计 — 全中国单节点方案

> 隆小侠 LONG CLAW — Docker + GitHub Actions 一键部署

## 1. 问题背景

### 1.1 核心矛盾

| 数据源 | 中国大陆 | 海外 | 结论 |
|--------|---------|------|------|
| **Futu A股** | 免费 LV1 | **完全不支持** | 必须大陆 |
| **Futu 港股** | 免费 LV2 | 仅 LV1 | 大陆更优 |
| **Futu 美股** | 需付费订阅（IP 无关） | 同左 | 均可 |
| **AKShare** | 最稳定 | 偶有限流 | 大陆更优 |
| **pytdx/BaoStock** | 原生可用 | 不稳定 | 必须大陆 |
| **yfinance** | ❌ Yahoo 被 GFW 封锁 | 全球可用 | 大陆不可用 |
| **Alpaca** | ❌ 被 GFW 封锁 | 全球可用 | 大陆不可用 |
| **IB Gateway** | ⚠️ 需境外网关 | 全球可用 | 大陆受限 |

**结论**:
- A股/港股数据 → 中国大陆是**硬性需求**
- 美股数据 → Futu 可覆盖（需付费订阅），AKShare 也有部分美股数据可兜底
- yfinance/Alpaca/IB 在大陆不可用，但 Futu 完全可替代

### 1.2 当前架构（单机 CLI）

```
┌─────────────────────────────────────────────┐
│  Mac 本地 (python run.py)                     │
│                                              │
│  ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐  │
│  │ Data │──▶│  L1  │──▶│  L2  │──▶│  L3  │  │
│  │ 多源 │   │ 指数 │   │ 行业 │   │ 标的 │  │
│  └──────┘   └──────┘   └──────┘   └──────┘  │
│      │                                │      │
│      ▼                                ▼      │
│  Futu/AKShare/                   飞书推送     │
│  pytdx/yfinance                              │
└─────────────────────────────────────────────┘
```

**痛点**: Futu 需要大陆 IP，但 Mac 在美国。

### 1.3 方案选择

| 方案 | 复杂度 | 延迟 | 成本 | 选择 |
|------|--------|------|------|------|
| **全中国单节点** | ⭐ 最简单 | 无跨区 | ¥50-100/月 | ✅ 采用 |
| 中国数据 + Vercel 分析 | ⭐⭐⭐ | 200ms RTT | ¥50 + $20 | ❌ |
| 中国数据 + 港新中转 | ⭐⭐ | 30ms RTT | ¥50 + $5 | ❌ |
| 中国数据 + Mac 本地 | ⭐⭐ | 200ms RTT | ¥50 | ❌ |

**全中国部署的优势**:
- 零跨区通信延迟
- 单节点运维，最简单
- A股/港股数据源全部原生可用
- 美股用 Futu（付费）或 AKShare 覆盖
- 飞书推送从中国发出，延迟更低

---

## 2. 目标架构

### 2.1 总体设计

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Repository                         │
│                  (代码管理 + CI/CD)                           │
└────────────────────────┬────────────────────────────────────┘
                         │ push to main
                         │ GitHub Actions
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  🇨🇳 中国云 ECS (2C4G)                                       │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  Docker Compose                                      │    │
│  │                                                      │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │    │
│  │  │ Futu OpenD  │  │   signals   │  │    Redis    │  │    │
│  │  │  (数据网关)  │  │  (分析引擎)  │  │   (缓存)    │  │    │
│  │  │  TCP:11111  │  │             │  │  TCP:6379   │  │    │
│  │  └──────┬──────┘  │  ┌───────┐  │  └──────┬──────┘  │    │
│  │         │         │  │  L1   │  │         │         │    │
│  │         ├────────▶│  │  L2   │  │◀────────┘         │    │
│  │         │         │  │  L3   │  │                   │    │
│  │  ┌──────┴──────┐  │  └───┬───┘  │                   │    │
│  │  │  AKShare    │  │      │      │                   │    │
│  │  │  pytdx      │──▶      │      │                   │    │
│  │  │  BaoStock   │  │      ▼      │                   │    │
│  │  └─────────────┘  │  ┌───────┐  │                   │    │
│  │                   │  │ Notify │──┼──▶ 飞书推送       │    │
│  │                   │  └───────┘  │                   │    │
│  │                   └─────────────┘                   │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │  Cron (systemd)  │  │  SQLite 持久存储  │                  │
│  │  定时触发分析     │  │  minute_cache.db  │                  │
│  └──────────────────┘  └──────────────────┘                  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心变化

与当前本地 CLI 相比，**代码几乎不需要改**:

| 维度 | 现在 (Mac CLI) | 目标 (中国云) | 改动 |
|------|---------------|-------------|------|
| 运行方式 | `python run.py` | Docker 内 `python run.py` | 无 |
| 数据源 | Futu+AKShare+yfinance | Futu+AKShare (移除 yfinance) | 极小 |
| 缓存 | 本地 SQLite | 本地 SQLite + Redis | 可选 |
| 调度 | 手动运行 | systemd cron 定时 | 新增 |
| 通知 | 飞书 | 飞书（不变） | 无 |
| 部署 | 手动 | GitHub Actions → SSH → Docker | 新增 |
| 代码管理 | 本地 Git | GitHub (不变) | 无 |

**关键洞察**: 这不是重新设计架构，而是把现有 CLI 原封不动搬到云上跑。

---

## 3. 服务器选型

### 3.1 云服务商对比

| 对比项 | 阿里云轻量 | 腾讯云轻量 | 华为云 |
|--------|-----------|-----------|--------|
| **推荐配置** | 2C4G | 2C4G | 2C4G |
| **月费** | ¥52-98 | ¥45-88 | ¥60-100 |
| **Docker** | 原生支持 | 原生支持 | 原生支持 |
| **GitHub SSH** | ✅ | ✅ | ✅ |
| **Futu 兼容** | ✅ | ✅ (腾讯系) | ✅ |
| **备案** | 不需要 | 不需要 | 不需要 |
| **特点** | 生态最全 | 与 Futu 同系 | — |

> **备案说明**: 不对外提供 Web 服务，仅内部 cron 运行 + SSH 管理，无需 ICP 备案。

### 3.2 推荐配置

```
最低配置:  2C2G 40GB SSD    ¥45/月  (够用，但 czsc 分析时内存偏紧)
推荐配置:  2C4G 60GB SSD    ¥80/月  (分析时内存充裕，Docker 运行舒适)
可选升级:  4C8G 80GB SSD    ¥150/月 (如需多市场并行分析)
```

### 3.3 为什么不需要高配

```
CPU:  czsc 分析是顺序处理，2C 足够 (ThreadPoolExecutor 主要等 I/O)
内存: Python + czsc + AKShare ≈ 800MB，4G 足够跑 Docker 全套
磁盘: SQLite 缓存 + Docker 镜像 ≈ 10GB，40GB 够用
带宽: AKShare/Futu 数据量小 (每次分析 <10MB)，1Mbps 够用
```

---

## 4. Docker 部署设计

### 4.1 目录结构

```
signals/
├── deploy/
│   ├── Dockerfile                  # 主应用镜像
│   ├── docker-compose.yml          # 编排: signals + futu + redis
│   ├── docker-compose.dev.yml      # 开发覆盖 (可选)
│   ├── cron/
│   │   └── signals-cron            # crontab 定时任务配置
│   ├── scripts/
│   │   ├── entrypoint.sh           # 容器入口 (处理信号、日志)
│   │   └── healthcheck.sh          # 健康检查脚本
│   └── .env.example                # 环境变量模板
├── .github/
│   └── workflows/
│       ├── deploy.yml              # 推送 main → 自动部署中国服务器
│       └── cron-trigger.yml        # (可选) GitHub Actions 定时触发
└── ... (现有代码不变)
```

### 4.2 Dockerfile

```dockerfile
# deploy/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 系统依赖 (czsc 需要 gcc 编译)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ cron && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 数据目录
RUN mkdir -p /app/.data /app/logs

# 入口
COPY deploy/scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "run.py"]
```

### 4.3 docker-compose.yml

```yaml
# deploy/docker-compose.yml
version: "3.8"

services:
  # ── Futu OpenD 数据网关 ──────────────────────
  futu-opend:
    image: futuopend/ftopend:latest
    container_name: futu-opend
    environment:
      - login_account=${FUTU_ACCOUNT}
      - login_pwd_md5=${FUTU_PWD_MD5}
    ports:
      - "11111:11111"        # 仅内部网络需要，不暴露外网
    restart: unless-stopped
    volumes:
      - futu-data:/home/futu/data
    healthcheck:
      test: ["CMD", "nc", "-z", "localhost", "11111"]
      interval: 30s
      timeout: 5s
      retries: 3

  # ── 分析引擎 (主应用) ────────────────────────
  signals:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    container_name: signals
    environment:
      - FUTU_HOST=futu-opend
      - FUTU_PORT=11111
      - REDIS_URL=redis://redis:6379
      - FEISHU_APP_ID=${FEISHU_APP_ID}
      - FEISHU_APP_SECRET=${FEISHU_APP_SECRET}
      - FEISHU_RECEIVE_ID=${FEISHU_RECEIVE_ID}
      - DEPLOY_MODE=cloud
      - TZ=Asia/Shanghai
    depends_on:
      futu-opend:
        condition: service_healthy
    restart: unless-stopped
    volumes:
      - signals-data:/app/.data     # SQLite 缓存持久化
      - signals-logs:/app/logs      # 日志持久化
      - ./cron/signals-cron:/etc/cron.d/signals-cron  # 定时任务

  # ── Redis 缓存 (可选，Phase 2) ──────────────
  redis:
    image: redis:7-alpine
    container_name: redis
    restart: unless-stopped
    volumes:
      - redis-data:/data
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

volumes:
  futu-data:
  signals-data:
  redis-data:
  signals-logs:
```

### 4.4 定时任务配置

```bash
# deploy/cron/signals-cron
# ── A股/港股 盘中扫描 (北京时间) ──────────────────
# 开盘前预热 (9:25)
25 9 * * 1-5  cd /app && python run.py --mode index >> /app/logs/cron.log 2>&1

# 盘中扫描 (9:45, 10:30, 11:15, 13:30, 14:30)
45 9 * * 1-5   cd /app && python run.py >> /app/logs/cron.log 2>&1
30 10 * * 1-5  cd /app && python run.py >> /app/logs/cron.log 2>&1
15 11 * * 1-5  cd /app && python run.py >> /app/logs/cron.log 2>&1
30 13 * * 1-5  cd /app && python run.py >> /app/logs/cron.log 2>&1
30 14 * * 1-5  cd /app && python run.py >> /app/logs/cron.log 2>&1

# 盘后复盘 (15:15)
15 15 * * 1-5  cd /app && python run.py --mode review >> /app/logs/cron.log 2>&1

# ── 美股扫描 (北京时间 = EST+13) ──────────────────
# 美股开盘 21:30 EST → 次日 10:30 CST (夏令时 9:30)
30 22 * * 1-5  cd /app && python run.py --market us >> /app/logs/cron.log 2>&1
00 0 * * 2-6   cd /app && python run.py --market us >> /app/logs/cron.log 2>&1

# ── 日志清理 (每周日) ────────────────────────────
0 3 * * 0  find /app/logs -name "*.log" -mtime +30 -delete
```

### 4.5 入口脚本

```bash
#!/bin/bash
# deploy/scripts/entrypoint.sh

set -e

echo "🐲 隆小侠 LONG CLAW — 云端启动"
echo "  时区: $(date +'%Z %Y-%m-%d %H:%M:%S')"
echo "  Futu: ${FUTU_HOST}:${FUTU_PORT}"
echo "  模式: ${DEPLOY_MODE:-local}"

# 如果是 cron 模式，启动 cron 守护进程
if [ "$1" = "cron" ]; then
    echo "  启动定时任务模式..."
    crontab /etc/cron.d/signals-cron
    cron -f  # 前台运行 cron
else
    # 直接执行传入的命令
    exec "$@"
fi
```

---

## 5. 美股数据方案

### 5.1 GFW 影响分析

在中国大陆部署后，美股数据源可用性:

| 数据源 | 可用性 | 说明 |
|--------|--------|------|
| **Futu 美股** | ✅ 可用 | 需购买美股 LV1 行情（¥100/月起） |
| **AKShare 美股** | ✅ 可用 | 爬取东财美股数据，免费但可能延迟 |
| **yfinance** | ❌ 不可用 | Yahoo 被 GFW 封锁 |
| **Alpaca** | ❌ 不可用 | API 被 GFW 封锁 |
| **IB Gateway** | ⚠️ 需隧道 | IB 服务器在境外 |

### 5.2 推荐: Futu 统一所有市场

```
当前降级链:
  美股: IB → Futu → yfinance (兜底)

调整为:
  美股: Futu → AKShare美股 (兜底)
```

Futu 美股行情订阅:
- **Nasdaq Basic**: ~¥100/月，覆盖所有美股实时行情
- SPY/QQQ/DIA 三只 ETF 的日线+分钟线完全够用
- 个股如果不需要实时，AKShare 东财美股可兜底（15min 延迟）

### 5.3 代码改动

```python
# signals/data/us_factory.py — 调整降级链
def create_us_source(mode: str = "intraday", futu_source=None) -> USDataSource:
    providers = []

    if os.environ.get("DEPLOY_MODE") == "cloud":
        # 云端模式: Futu 优先，AKShare 兜底（无 yfinance/IB/Alpaca）
        if futu_source:
            providers.append(futu_source)
        providers.append(AKShareUSSource())  # 新增: AKShare 美股
    else:
        # 本地模式: 保持现有降级链
        providers.append(IBSource(...))
        if futu_source:
            providers.append(futu_source)
        providers.append(YFinanceSource())

    return USDataSource(providers=providers)
```

**改动量**: 仅 `us_factory.py` 中加一个 `if` 分支，~10 行代码。

---

## 6. GitHub Actions CI/CD

### 6.1 自动部署流程

```
开发者 push 代码到 main
        │
        ▼
GitHub Actions 触发
        │
        ├─ 1. 代码检查 (lint/test)
        │
        ├─ 2. SSH 连接中国服务器
        │
        ├─ 3. git pull 最新代码
        │
        ├─ 4. docker-compose build
        │
        └─ 5. docker-compose up -d (滚动重启)
```

### 6.2 deploy.yml

```yaml
# .github/workflows/deploy.yml
name: Deploy to China Cloud

on:
  push:
    branches: [main]
    paths-ignore:
      - 'docs/**'
      - '*.md'
      - 'tests/**'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.CN_SERVER_IP }}
          username: ${{ secrets.CN_SERVER_USER }}
          key: ${{ secrets.CN_SSH_KEY }}
          script: |
            set -e
            cd /opt/signals

            # 拉取最新代码
            git fetch origin main
            git reset --hard origin/main

            # 重建并重启
            cd deploy
            docker-compose build --no-cache signals
            docker-compose up -d

            # 验证
            sleep 5
            docker-compose ps
            docker-compose logs --tail=20 signals

      - name: Notify on failure
        if: failure()
        run: |
          curl -X POST "https://open.feishu.cn/open-apis/bot/v2/hook/${{ secrets.FEISHU_WEBHOOK }}" \
            -H "Content-Type: application/json" \
            -d '{"msg_type": "text", "content": {"text": "⚠️ 部署失败，请检查 GitHub Actions"}}'
```

### 6.3 GitHub Secrets 配置

```
GitHub Repository Settings → Secrets and variables → Actions

必需:
├── CN_SERVER_IP          # 中国 ECS 公网 IP
├── CN_SERVER_USER        # SSH 用户名 (如 root 或 deploy)
├── CN_SSH_KEY            # SSH 私钥 (ed25519 推荐)
├── FUTU_ACCOUNT          # Futu 登录账号
├── FUTU_PWD_MD5          # Futu 密码 MD5
├── FEISHU_APP_ID         # 飞书机器人凭证
├── FEISHU_APP_SECRET
└── FEISHU_RECEIVE_ID

可选:
├── FEISHU_WEBHOOK        # 飞书 Webhook (部署通知)
└── FUTU_US_SUBSCRIPTION  # 美股行情订阅类型
```

---

## 7. 服务器初始化指南

### 7.1 一键初始化脚本

```bash
#!/bin/bash
# deploy/scripts/server-init.sh
# 用法: ssh root@<IP> 'bash -s' < deploy/scripts/server-init.sh

set -e
echo "🐲 隆小侠服务器初始化..."

# 1. 系统更新
apt-get update && apt-get upgrade -y

# 2. 安装 Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker && systemctl start docker

# 3. 安装 Docker Compose
apt-get install -y docker-compose-plugin

# 4. 安装 Git
apt-get install -y git

# 5. 配置时区
timedatectl set-timezone Asia/Shanghai

# 6. 创建工作目录
mkdir -p /opt/signals
cd /opt/signals

# 7. 克隆代码
git clone https://github.com/<YOUR_USER>/signals.git .

# 8. 创建 .env 文件
cp deploy/.env.example deploy/.env
echo "请编辑 /opt/signals/deploy/.env 填写凭证"
echo "然后运行: cd /opt/signals/deploy && docker-compose up -d"
```

### 7.2 首次部署

```bash
# 1. 编辑环境变量
vim /opt/signals/deploy/.env

# 2. 启动所有服务
cd /opt/signals/deploy
docker-compose up -d

# 3. 查看状态
docker-compose ps
docker-compose logs -f signals

# 4. 手动测试一次分析
docker-compose exec signals python run.py --mode index

# 5. 启动定时任务
docker-compose exec signals bash -c "crontab /etc/cron.d/signals-cron && cron"
```

---

## 8. 运维与监控

### 8.1 日常运维命令

```bash
# 查看日志
docker-compose logs -f signals          # 实时日志
docker-compose logs --tail=100 signals  # 最近 100 行
cat /opt/signals/logs/cron.log          # 定时任务日志

# 手动运行
docker-compose exec signals python run.py                # 盘中扫描
docker-compose exec signals python run.py --mode index   # 仅看指数
docker-compose exec signals python run.py --market us    # 美股

# 重启服务
docker-compose restart signals          # 重启分析引擎
docker-compose restart futu-opend       # 重启 Futu

# 更新代码 (手动)
cd /opt/signals && git pull origin main
cd deploy && docker-compose build signals && docker-compose up -d signals
```

### 8.2 健康检查

```bash
# deploy/scripts/healthcheck.sh
#!/bin/bash

# 检查 Futu OpenD
if ! nc -z futu-opend 11111 2>/dev/null; then
    echo "❌ Futu OpenD 不可达"
    # 发送飞书告警
    exit 1
fi

# 检查最近一次分析是否成功 (看日志最后一次)
LAST_RUN=$(grep "分析完成" /app/logs/cron.log | tail -1)
if [ -z "$LAST_RUN" ]; then
    echo "⚠️ 今日尚无成功的分析记录"
fi

echo "✅ 服务正常"
```

### 8.3 飞书告警 (异常自动通知)

```python
# 可选: 在 run.py 顶层加 try/except
# 分析异常时自动推送飞书告警

try:
    main()
except Exception as e:
    from signals.notify import send_text
    send_text(f"⚠️ 分析异常: {e}\n请检查服务器日志")
    raise
```

---

## 9. 代码改动清单

### 9.1 新增文件

| 文件 | 用途 | 复杂度 |
|------|------|--------|
| `deploy/Dockerfile` | 主应用 Docker 镜像 | 低 |
| `deploy/docker-compose.yml` | 服务编排 | 低 |
| `deploy/cron/signals-cron` | 定时任务配置 | 低 |
| `deploy/scripts/entrypoint.sh` | 容器入口 | 低 |
| `deploy/scripts/server-init.sh` | 服务器初始化 | 低 |
| `deploy/scripts/healthcheck.sh` | 健康检查 | 低 |
| `deploy/.env.example` | 环境变量模板 | 低 |
| `.github/workflows/deploy.yml` | CI/CD 自动部署 | 中 |

### 9.2 修改文件

| 文件 | 改动 | 行数 |
|------|------|------|
| `config.py` | 新增 `DEPLOY_MODE` 环境变量 | +3 行 |
| `signals/data/us_factory.py` | 云端模式跳过 yfinance/IB/Alpaca | +10 行 |
| `requirements.txt` | 确保无系统依赖冲突 | 检查 |
| `.gitignore` | 添加 `deploy/.env`, `logs/` | +2 行 |

### 9.3 不需要改的（核心保持不变）

- `signals/core/` — 分析引擎
- `signals/layers/` — L1/L2/L3 全部逻辑
- `signals/data/fetcher.py` — 数据源（Futu/AKShare 照常用）
- `signals/notify/` — 飞书通知
- `run.py` — 主入口（不改，docker 内直接运行）

**总改动量: ~8 个新文件 + ~15 行代码修改**

---

## 10. 分阶段实施计划

### Phase 1: Docker 化 (Day 1)

```
目标: 本地 docker-compose up 能跑通分析

步骤:
1. 创建 Dockerfile + docker-compose.yml
2. 本地测试: docker-compose up signals (不含 Futu)
3. 验证: python run.py --mode index 在容器内正常运行

验收: 容器内分析流程跑通 (仅 AKShare 数据)
```

### Phase 2: 服务器部署 (Day 2-3)

```
目标: 中国 ECS 上运行完整分析流程

步骤:
1. 购买 ECS (2C4G), 运行 server-init.sh
2. 配置 .env (Futu 凭证 + 飞书凭证)
3. docker-compose up -d (含 Futu OpenD)
4. 手动测试: docker-compose exec signals python run.py

验收: 飞书收到分析推送
```

### Phase 3: 定时任务 (Day 3)

```
目标: 盘中自动分析 + 推送

步骤:
1. 配置 crontab 定时任务
2. 测试: 等待下一个交易日观察
3. 验证日志: tail -f logs/cron.log

验收: 工作日自动收到飞书推送
```

### Phase 4: CI/CD (Day 4)

```
目标: 推代码到 main → 自动部署到服务器

步骤:
1. 配置 GitHub Secrets (SSH 密钥等)
2. 创建 .github/workflows/deploy.yml
3. 推送测试变更 → 验证自动部署

验收: git push → 服务器自动更新 → 飞书通知
```

### Phase 5: 美股数据 (可选, Day 5+)

```
目标: 美股数据通过 Futu 覆盖

步骤:
1. 购买 Futu 美股行情订阅
2. 调整 us_factory.py 降级链
3. 测试美股指数 (SPY/QQQ/DIA) 分析

验收: 美股分析结果正常推送
```

---

## 11. 成本估算

| 项目 | 月费用 | 说明 |
|------|--------|------|
| 云服务器 2C4G | ¥60-100 | 阿里云/腾讯云轻量 |
| Futu 美股行情 (可选) | ¥0-100 | 如不需要美股实时数据可省 |
| GitHub Actions | ¥0 | 免费 2000 min/月 |
| **总计** | **¥60-200/月** | |

---

## 12. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Futu OpenD Docker 不稳定 | 中 | A/HK 数据降级 | 降级到 AKShare |
| ECS 重启/维护 | 低 | 暂停分析 | Docker restart policy + 飞书告警 |
| Futu 账号被踢 (多设备登录) | 中 | 数据断供 | 使用独立账号 |
| 磁盘满 (SQLite/日志增长) | 低 | 服务异常 | 定期清理 cron + 磁盘告警 |
| GitHub → 中国 SSH 连接不稳 | 低 | 部署失败 | 重试机制 + 手动部署备选 |

---

## 13. 未来扩展路径

### 13.1 如果需要远程访问分析结果

```
Phase 2 扩展: 加一个轻量 FastAPI 服务

docker-compose 新增:
  web:
    ports: "8000:8000"
    command: uvicorn api:app --host 0.0.0.0

提供:
  GET /api/results     → 最新分析结果 (JSON)
  GET /api/health      → 服务状态
  POST /api/trigger    → 手动触发分析
```

### 13.2 如果需要从 Mac 远程触发

```
# Mac 上运行:
ssh root@<CN_IP> "cd /opt/signals && docker-compose exec signals python run.py"

# 或通过 API:
curl -X POST http://<CN_IP>:8000/api/trigger -H "X-API-Key: xxx"
```

### 13.3 如果需要双区域部署 (未来)

当前全中国方案保留了完整的 `SimDataSource` 注入模式。未来如果需要:
- 美国端 (Vercel/Mac) 调用中国数据 → 只需加 `RemoteDataSource` + `deploy/cn-server/server.py`
- 中国端代码完全不改
- 这就是文档第一版设计的方案，可以随时切换

---

## 附录 A: 环境变量模板

```bash
# deploy/.env.example

# ── Futu OpenD ──────────────────────
FUTU_ACCOUNT=your_futu_account
FUTU_PWD_MD5=your_password_md5

# ── 飞书机器人 ──────────────────────
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_RECEIVE_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxx

# ── 部署模式 ────────────────────────
DEPLOY_MODE=cloud
TZ=Asia/Shanghai

# ── Futu 美股 (可选) ────────────────
# FUTU_US_SUBSCRIPTION=nasdaq_basic
```

## 附录 B: 本地开发 vs 云端部署对比

```
┌─────────────────────────────────────────────────────────┐
│  开发流程 (不变)                                          │
│                                                          │
│  Mac 本地开发:                                            │
│    python run.py               → 本地直接运行             │
│    python run.py --mode sim    → 仿真回放                 │
│                                                          │
│  云端部署:                                                │
│    git push origin main        → 自动部署到中国云         │
│    docker-compose exec signals python run.py  → 手动触发  │
│    cron 自动运行               → 定时推送飞书             │
│                                                          │
│  两种模式共用同一套代码，通过 DEPLOY_MODE 区分。             │
└─────────────────────────────────────────────────────────┘
```
