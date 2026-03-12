# API router registry: mounts all route modules in one place.
# Keeps the app entrypoint minimal and organized.

from fastapi import APIRouter

from api.routes.project_setup import router as project_setup_router
from api.routes.progress import router as progress_router
from api.routes.tour_management import router as tour_management_router
from api.routes.features import router as features_router
from api.routes.ai_management import router as ai_management_router

api_router = APIRouter()

api_router.include_router(project_setup_router)
api_router.include_router(progress_router)
api_router.include_router(tour_management_router)
api_router.include_router(features_router)
api_router.include_router(ai_management_router)
