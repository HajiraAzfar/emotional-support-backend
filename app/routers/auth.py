from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.devices import register_device
from app.core.rate_limit import clear_failures, is_rate_limited, record_failure
from app.core.security import hash_password, verify_password
from app.core.tokens import issue_token_pair, revoke_all_sessions, rotate_refresh_token
from app.core.verification import (
    redeem_token,
    send_password_reset_email,
    send_verification_email,
)
from app.models.account import Account
from app.schemas.account import (
    AccountResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(Account).filter(Account.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please sign in instead.",
        )

    account = Account(
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    send_verification_email(db, account.id, account.email)

    tokens = issue_token_pair(db, account.id)
    return {**tokens, "account": account}


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
    x_device_id: str | None = Header(default=None),
):
    if is_rate_limited(payload.email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Please try again later.",
        )

    account = db.query(Account).filter(Account.email == payload.email).first()

    if account is None or not verify_password(payload.password, account.password_hash):
        record_failure(payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    clear_failures(payload.email)

    register_device(db, account.id, account.email, x_device_id)

    tokens = issue_token_pair(db, account.id)
    return {**tokens, "account": account}


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    tokens = rotate_refresh_token(db, payload.refresh_token)

    if tokens is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )

    account = db.query(Account).filter(Account.id == tokens["account_id"]).first()
    return {**tokens, "account": account}


@router.get("/me", response_model=AccountResponse)
def me(account: Account = Depends(get_current_user)):
    return account


@router.get("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    account_id = redeem_token(db, token, "verify_email")

    if account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link is invalid or has expired.",
        )

    account = db.query(Account).filter(Account.id == account_id).first()
    account.verified = True
    db.commit()

    return {"detail": "Your email address has been confirmed."}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.email == payload.email).first()

    if account is not None:
        send_password_reset_email(db, account.id, account.email)

    return {"detail": "If an account exists for that address, a reset link has been sent."}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    account_id = redeem_token(db, payload.token, "password_reset")

    if account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link is invalid or has expired.",
        )

    account = db.query(Account).filter(Account.id == account_id).first()
    account.password_hash = hash_password(payload.password)
    db.commit()

    revoke_all_sessions(db, account_id)
    clear_failures(account.email)

    return {"detail": "Your password has been changed. Please sign in again."}