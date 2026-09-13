import random
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


def create_code(db: Session, account_id, purpose: str, minutes: int) -> str:
    """Generate a 6-digit numeric code, store its hash, return the raw code."""
    code = f"{random.randint(0, 999999):06d}"

    record = VerificationToken(
        account_id=account_id,
        token_hash=hash_refresh_token(code),
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
    )
    db.add(record)
    db.commit()

    print(f"[DEBUG] Code for account {account_id} ({purpose}): {code}")  # TEMPORARY — remove before production

    return code


def redeem_code(db: Session, account_id, raw_code: str, purpose: str):
    """Redeem a 6-digit code, scoped to a specific account (unlike link tokens,
    which are unique enough to look up alone, a 6-digit code is not)."""
    record = (
        db.query(VerificationToken)
        .filter(
            VerificationToken.account_id == account_id,
            VerificationToken.token_hash == hash_refresh_token(raw_code),
            VerificationToken.purpose == purpose,
        )
        .first()
    )

    if record is None or record.used or record.expires_at < datetime.utcnow():
        return None

    record.used = True
    db.commit()

    return record.account_id


def send_signup_code_email(db: Session, account_id, email: str) -> bool:
    code = create_code(db, account_id, "signup_verify", minutes=15)

    return send_email(
        to=email,
        subject="Your verification code",
        html=(
            f'<p>Your verification code is:</p>'
            f'<p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">{code}</p>'
            f'<p>This code expires in 15 minutes.</p>'
            f'<p>If you did not request this, you can ignore this message.</p>'
        ),
    )


def send_already_registered_email(email: str) -> None:
    send_email(
        to=email,
        subject="Sign in to your account",
        html=(
            f'<p>An account already exists for this email address.</p>'
            f'<p>Please sign in instead.</p>'
        ),
    )


def send_reset_code_email(db: Session, account_id, email: str) -> None:
    code = create_code(db, account_id, "password_reset_code", minutes=15)

    send_email(
        to=email,
        subject="Your password reset code",
        html=(
            f'<p>Your password reset code is:</p>'
            f'<p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">{code}</p>'
            f'<p>This code expires in 15 minutes.</p>'
            f'<p>If you did not request this, you can ignore this message and your '
            f'password will stay the same.</p>'
        ),
    )


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