"""健康检查接口"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

router = APIRouter()


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查：验证应用与数据库连接"""
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar_one()
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    return {
        "status": "ok",
        "service": "Yuan-Meet · 研发会议垂类 Agent API",
        "database": db_status,
    }
