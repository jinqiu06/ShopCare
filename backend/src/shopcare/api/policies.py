from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_session
from shopcare.schemas.policy_models import PolicySearchHit
from shopcare.services.policy_retriever import search_policy_chunks

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("/search", response_model=list[PolicySearchHit])
def search_policies(
    q: str = Query(..., min_length=1, max_length=500),
    category: str = Query(..., min_length=1, max_length=50),
    as_of: date = Query(...),
    top_k: int = Query(default=3, ge=1, le=10),
    session: Session = Depends(get_session),
):
    return search_policy_chunks(
        session=session,
        query=q,
        category=category,
        as_of=as_of,
        top_k=top_k,
    )
