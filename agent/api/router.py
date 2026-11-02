"""Mount the Agent API components under the existing /api/agent prefix."""
from fastapi import APIRouter
from .investigations import router as investigations_router
from .review import router as review_router
from .handoff import router as handoff_router
from agent.patient_memory.api import router as memory_router
from agent.patient_memory.progress.api import router as progress_router

from agent.patient_memory.actions.api import router as actions_router

router = APIRouter(prefix="/api/agent", tags=["agent"])
router.include_router(investigations_router)
router.include_router(review_router)
router.include_router(handoff_router)
router.include_router(memory_router)
router.include_router(progress_router)

router.include_router(actions_router)
