"""
The learning library (SRS 4.11). Read-only content plus the articles she saved.
Nothing here is generated: every word comes from content/library/.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import learning
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.library_favourite import LibraryFavourite

router = APIRouter(prefix="/library", tags=["library"])


def _saved(db: Session, account_id) -> set[str]:
    return {
        row.slug
        for row in db.query(LibraryFavourite).filter(LibraryFavourite.account_id == account_id).all()
    }


@router.get("")
def list_articles(
    q: str | None = Query(None, max_length=100),
    trap: str | None = Query(None, max_length=60),
    category: str | None = Query(None, max_length=40),
    saved_only: bool = False,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Everything, or what matches the search, with her saved ones marked."""
    saved = _saved(db, account.id)
    found = learning.search(q, trap, category)
    if saved_only:
        found = [article for article in found if article.slug in saved]
    return {
        "categories": learning.CATEGORIES,
        "featured": [a.listed(a.slug in saved) for a in found if a.featured],
        "articles": [a.listed(a.slug in saved) for a in found],
        "saved_count": len(saved),
    }


@router.get("/{slug}")
def read_article(
    slug: str,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    article = learning.get(slug)
    if article is None:
        raise HTTPException(status_code=404, detail="No such article")
    return article.full(slug in _saved(db, account.id))


@router.post("/{slug}/favourite", status_code=status.HTTP_201_CREATED)
def save_article(
    slug: str,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    if learning.get(slug) is None:
        raise HTTPException(status_code=404, detail="No such article")
    existing = (
        db.query(LibraryFavourite)
        .filter(LibraryFavourite.account_id == account.id, LibraryFavourite.slug == slug)
        .first()
    )
    if existing is None:
        db.add(LibraryFavourite(account_id=account.id, slug=slug))
        db.commit()
    return {"favourite": True}


@router.delete("/{slug}/favourite")
def unsave_article(
    slug: str,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    db.query(LibraryFavourite).filter(
        LibraryFavourite.account_id == account.id, LibraryFavourite.slug == slug
    ).delete()
    db.commit()
    return {"favourite": False}
