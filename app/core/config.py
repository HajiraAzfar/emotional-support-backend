from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    CONSENT_VERSION: str = "v1"
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    RESEND_API_KEY: str
    EMAIL_FROM: str = "onboarding@resend.dev"
    APP_BASE_URL: str = "http://127.0.0.1:8000"
    

    class Config:
        env_file = ".env"


settings = Settings()