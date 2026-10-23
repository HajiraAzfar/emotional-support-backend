import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccountResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    verified: bool
    weekly_goal: int
    interface_language: str
    preferred_name: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    account: AccountResponse


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)

class SignupEmailRequest(BaseModel):
    email: EmailStr


class VerifySignupCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class SetupTokenResponse(BaseModel):
    setup_token: str


class SetPasswordRequest(BaseModel):
    setup_token: str
    password: str = Field(min_length=8)


class VerifyResetCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class ResetTokenResponse(BaseModel):
    reset_token: str


class ResetPasswordCodeRequest(BaseModel):
    reset_token: str
    password: str = Field(min_length=8)


class DeleteAccountRequest(BaseModel):
    # Deleting everything asks for the password again, so an unlocked phone in
    # someone else's hands cannot do it.
    password: str = Field(min_length=1, max_length=200)
    