# -*- coding: utf-8 -*-
"""
🐲 隆小侠 Web UI — FastAPI 应用

启动方式: python run.py --mode web [--port 8000]
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .api.index import router as index_router
from .api.chart import router as chart_router
from .api.screener import router as screener_router
from .api.industry import router as industry_router
from .api.stock import router as stock_router
from .api.analog import router as analog_router
from .api.review import router as review_router
from .api.backtest import router as backtest_router
from .api.social import router as social_router
from .api.plan import router as plan_router
from .api.trade import router as trade_router
from .api.prediction import router as prediction_router
from .api.workbench import router as workbench_router
from signals.web2.api.cluster import start_scheduler as start_cluster_scheduler
from signals.web2.api.cluster import stop_scheduler as stop_cluster_scheduler
from .services.engine import get_engine

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    app = FastAPI(
        title="隆小侠 LONG CLAW",
        description="缠论三层联动分析 Web UI",
        version="0.1.0",
    )

    # CORS（开发阶段宽松，Electron 迁移时也需要 localhost 跨域）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册 API 路由
    app.include_router(index_router)
    app.include_router(chart_router)
    app.include_router(screener_router)
    app.include_router(industry_router)
    app.include_router(stock_router)
    app.include_router(analog_router)
    app.include_router(review_router)
    app.include_router(backtest_router)
    app.include_router(social_router)
    app.include_router(plan_router)
    app.include_router(trade_router)
    app.include_router(prediction_router)
    app.include_router(workbench_router)

    # 静态文件服务
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.on_event("startup")
    async def startup_event():
        get_engine().run_all_async()
        start_cluster_scheduler()

    @app.on_event("shutdown")
    async def shutdown_event():
        stop_cluster_scheduler()

    # SPA 入口：所有非 /api/ 和 /static/ 的请求都返回 index.html
    @app.get("/")
    async def serve_workbench():
        return FileResponse(str(STATIC_DIR / "workbench.html"))

    @app.get("/legacy")
    async def serve_legacy():
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app
