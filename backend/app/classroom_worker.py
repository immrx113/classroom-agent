"""Personal classroom worker: python -m app.classroom_worker [--once]."""
import argparse
import asyncio
import logging

from sqlalchemy import select, text

from app.config import settings
from app.db.session import async_session_factory, engine
from app.models.course import Course
from app.models.meeting import Meeting
from app.services.asr_provider import get_provider
from app.services.job_service import recover_and_dispatch
from app.services.organize_service import organize_service
from app.services.index_service import index_service
from app.services.scope import ScopeContext

log = logging.getLogger(__name__)


async def run_cycle(*, provider=None, model=None, embed=None):
    # 回退只读（CA-25）：停写期 worker 周期不领取、不外发、不发布任何任务。
    # 各派发器已各自守卫，此处统一拦截，也覆盖 create_organize_job 的新建循环。
    from app.services.access import is_read_only

    if is_read_only():
        return {"asr": None, "organize": None, "index": None, "read_only": True}

    async with async_session_factory() as db:
        asr = await recover_and_dispatch(db, provider=provider or get_provider())
    async with async_session_factory() as db:
        lectures = (await db.execute(select(Meeting).join(Course).where(
            Course.owner_id == settings.LOCAL_SUBJECT_ID, Course.archived_at.is_(None),
            Meeting.active_transcript_version_id.is_not(None),
            Meeting.legacy_meeting.is_(False),
        ))).scalars().all()
        for lecture in lectures:
            await organize_service.create_organize_job(
                db, ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID),
                lecture_id=lecture.id, transcript_version_id=lecture.active_transcript_version_id)
        await db.commit()
    async with async_session_factory() as db:
        organized = await organize_service.recover_and_dispatch_organize(db, model=model)
    async with async_session_factory() as db:
        indexed = await index_service.recover_and_dispatch_index(db, embed=embed)
    return {"asr": asr, "organize": organized, "index": indexed}


async def main(once=False):
    from sqlalchemy.engine import make_url
    # 回退只读（CA-25）：停写期拒绝启动 worker 消费循环，不领取/不外发/不发布。
    from app.services.access import is_read_only

    if is_read_only():
        log.info("课堂已停写（回退只读），worker 拒绝启动，仅保留读取与取消能力")
        return
    if make_url(settings.DATABASE_URL).database != "yuan_classroom_local":
        raise RuntimeError("首轮worker仅允许连接yuan_classroom_local，拒绝处理原正式库")
    if not settings.CLASSROOM_PAID_CALLS_ENABLED or settings.ASR_MODE != "real":
        raise RuntimeError("请先显式开启课堂真实调用，并设置ASR_MODE=real")
    from app.services.oss_service import oss_service
    if not settings.OPENAI_API_KEY or not oss_service.is_available:
        raise RuntimeError("模型密钥或OSS未配置完整，未启动任务消费")
    from app.services import rate_limit_service
    await rate_limit_service.init_pool()
    try:
        # Hold one dedicated connection for the whole worker lifetime.
        async with engine.connect() as lock_conn:
            acquired = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(904291004)"))).scalar()
            if not acquired:
                raise RuntimeError("已有课堂worker正在运行，请勿重复启动")
            try:
                log.info("Classroom worker ready")
                while True:
                    log.info("课堂任务处理结果 %s", await run_cycle())
                    if once:
                        break
                    await asyncio.sleep(3)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(904291004)"))
        await engine.dispose()
    finally:
        await rate_limit_service.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本机课堂试用后台任务")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main(args.once))
    except KeyboardInterrupt:
        pass
