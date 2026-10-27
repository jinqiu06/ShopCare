from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/api/health")
def get_status() -> dict[str, str]:
    return {"status": "ok", "service": "shopcare"}
