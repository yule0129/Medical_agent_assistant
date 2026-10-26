"""Mount the Agent API components under the existing /api/agent prefix."""
from fastapi import APIRouter
from .investigations import router as investigations_router
from .review import router as review_router
from .handoff import router as handoff_router

router = APIRouter(prefix="/api/agent", tags=["agent"])
router.include_router(investigations_router)
router.include_router(review_router)
router.include_router(handoff_router)
