from fastapi import APIRouter, Depends

from app.api.v1.admin.groups import router as groups_router
from app.api.v1.admin.users import router as users_router
from app.core.deps import require_admin

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
router.include_router(users_router)
router.include_router(groups_router)
