# -*- coding: utf-8 -*-
"""隆小侠 Web2 — 精简版 FastAPI 应用（行业聚类 + MACD 回测）"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.cluster import router as cluster_router, start_scheduler, stop_scheduler
from .api.backtest import router as backtest_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子 — 管理聚类定时器。"""
    logger.info("🐲 隆小侠 Web2 启动")
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("🐲 隆小侠 Web2 关闭")


app = FastAPI(title="隆小侠 Web2", version="2.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 路由
app.include_router(cluster_router)
app.include_router(backtest_router)

# 静态文件
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# SPA fallback
@app.get("/{full_path:path}")
async def spa_fallback(request: Request, full_path: str):
    if full_path.startswith("api/"):
        return {"error": "not found"}
    return FileResponse(str(_STATIC / "index.html"))
