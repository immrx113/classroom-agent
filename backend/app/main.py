"""FastAPI 应用入口"""

import os
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.api import api_router
from app.services.errors import ServiceError, service_error_handler, http_error_handler
# 启动时触发 Tool Registry 注册（副作用 import）
import app.agents.tools.meeting_ops  # noqa: F401

# 配置 app logger（uvicorn 默认 root level=WARNING，需显式设置 INFO）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("app").setLevel(logging.INFO)
# HTTPX INFO logs include full signed ASR result URLs; keep their queries private.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动与关闭"""
    # 启动时
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    from app.services import rate_limit_service
    await rate_limit_service.init_pool()
    yield
    # 关闭时：关闭 Redis 连接池与 SFU 连接池等资源
    from app.services.sfu_bridge import sfu_bridge
    await rate_limit_service.close_pool()
    await sfu_bridge.aclose()


app = FastAPI(
    title=settings.APP_NAME,
    description="研发会议垂类 Agent 平台 API（评审决策 / 对齐闭环 / 复盘闭环）",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 统一错误契约（P02-16）
app.add_exception_handler(ServiceError, service_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)

# 注册路由
app.include_router(api_router)


@app.get("/")
async def root():
    """根路径"""
    return {"message": settings.APP_NAME, "docs": "/docs"}
