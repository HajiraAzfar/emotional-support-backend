from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session


from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.account import Account
from app.schemas.account import AccountResponse, LoginRequest, SignupRequest, TokenResponse
from app.core.dependencies import get_current_user
from app.core.rate_limit import clear_failures, is_rate_limited, record_failure

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

    return {"access_token": create_access_token(account.id), "account": account}


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
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

    return {"access_token": create_access_token(account.id), "account": account}

@router.get("/me", response_model=AccountResponse)
def me(account: Account = Depends(get_current_user)):
    return account