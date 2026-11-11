"""API 路由聚合"""

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.meetings import router as meetings_router
from app.api.summaries import router as summaries_router, global_router as summaries_global_router
from app.api.knowledge import router as knowledge_router
from app.api.chat import router as chat_router
from app.api.agent_runs import router as agent_runs_router
from app.api.rooms import router as rooms_router
from app.api.decisions import router as decisions_router
from app.api.courses import router as courses_router
from app.api.lectures import router as lectures_router
from app.api.evidence import router as evidence_router
from app.api.audio_assets import router as audio_assets_router
from app.api.jobs import router as jobs_router
from app.api.notes import router as notes_router
from app.api.documents import router as documents_router
from app.api.retrieval import router as retrieval_router
from app.api.diagnostics import router as diagnostics_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(meetings_router)
api_router.include_router(summaries_global_router)
api_router.include_router(summaries_router)
api_router.include_router(knowledge_router)
api_router.include_router(chat_router)
api_router.include_router(agent_runs_router)
api_router.include_router(rooms_router)
api_router.include_router(decisions_router)
api_router.include_router(courses_router)
api_router.include_router(lectures_router)
api_router.include_router(evidence_router)
api_router.include_router(audio_assets_router)
api_router.include_router(jobs_router)
api_router.include_router(notes_router)
api_router.include_router(documents_router)
api_router.include_router(retrieval_router)
api_router.include_router(diagnostics_router)

