from fastapi import APIRouter

from .create_project import router as create_project_router
from .project_management import router as project_management_router


router = APIRouter()
router.include_router(create_project_router)
router.include_router(project_management_router)
