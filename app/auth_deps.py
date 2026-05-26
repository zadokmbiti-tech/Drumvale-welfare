from fastapi import Depends, HTTPException, status
from app.routes.auth import get_current_user
ROLE_LEVELS = {
    "super_admin": 100,
    "chairperson": 40,
    "secretary":   30,
    "treasurer":   20,
    "member":      10,
}

def _require_level(minimum: int):
    async def dependency(current_user=Depends(get_current_user)):
        level = ROLE_LEVELS.get(current_user.get("role"), 0)
        if level < minimum:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to perform this action"
            )
        return current_user
    return dependency

require_member      = _require_level(10)
require_treasurer   = _require_level(20)
require_secretary   = _require_level(30)
require_chairperson = _require_level(40)
require_super_admin = _require_level(100)