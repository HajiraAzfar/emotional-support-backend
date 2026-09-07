import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from app.models.refresh_token import RefreshToken


def issue_token_pair(db: Session, account_id, family_id=None) -> dict:
    if family_id is None:
        family_id = uuid.uuid4()

    raw_token = generate_refresh_token()

    record = RefreshToken(
        account_id=account_id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()

    return {
         "access_token": create_access_token(account_id),
        "refresh_token": raw_token,
        "account_id": account_id,
        
    }


def revoke_family(db: Session, family_id) -> None:
    db.query(RefreshToken).filter(RefreshToken.family_id == family_id).update(
        {"revoked": True}
    )
    db.commit()
def rotate_refresh_token(db: Session, raw_token: str) -> dict | None:
    token_hash = hash_refresh_token(raw_token)
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()

    if record is None:
        return None

    if record.used or record.revoked:
        revoke_family(db, record.family_id)
        return None

    if record.expires_at < datetime.utcnow():
        return None

    record.used = True
    db.commit()

    return issue_token_pair(db, record.account_id, record.family_id)   