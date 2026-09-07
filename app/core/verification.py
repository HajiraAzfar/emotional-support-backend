from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email import send_email
from app.core.security import generate_refresh_token, hash_refresh_token
from app.models.verification_token import VerificationToken


def create_token(db: Session, account_id, purpose: str, hours: int) -> str:
    raw = generate_refresh_token()

    record = VerificationToken(
        account_id=account_id,
        token_hash=hash_refresh_token(raw),
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(hours=hours),
    )
    db.add(record)
    db.commit()

    return raw


def redeem_token(db: Session, raw: str, purpose: str):
    record = (
        db.query(VerificationToken)
        .filter(
            VerificationToken.token_hash == hash_refresh_token(raw),
            VerificationToken.purpose == purpose,
        )
        .first()
    )

    if record is None or record.used or record.expires_at < datetime.utcnow():
        return None

    record.used = True
    db.commit()

    return record.account_id


def send_verification_email(db: Session, account_id, email: str) -> None:
    raw = create_token(db, account_id, "verify_email", hours=24)
    link = f"{settings.APP_BASE_URL}/auth/verify-email?token={raw}"

    send_email(
        to=email,
        subject="Confirm your email address",
        html=(
            f'<p>Please confirm your email address by opening this link:</p>'
            f'<p><a href="{link}">Confirm email address</a></p>'
            f'<p>This link expires in 24 hours.</p>'
            f'<p>If you did not create an account, you can ignore this message.</p>'
        ),
    )

def send_password_reset_email(db: Session, account_id, email: str) -> None:
    raw = create_token(db, account_id, "password_reset", hours=1)
    link = f"{settings.APP_BASE_URL}/auth/reset-password?token={raw}"

    send_email(
        to=email,
        subject="Reset your password",
        html=(
            f'<p>You asked to reset your password. Open this link to choose a new one:</p>'
            f'<p><a href="{link}">Reset password</a></p>'
            f'<p>This link expires in one hour.</p>'
            f'<p>If you did not request this, you can ignore this message and your '
            f'password will stay the same.</p>'
        ),
    )