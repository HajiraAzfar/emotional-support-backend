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
    ELEVATED_DISTRESS_THRESHOLD: int = 9
    OPENAI_API_KEY: str | None = None
    # Leave unset for api.openai.com. For Azure: https://<resource>.openai.azure.com/openai/v1/
    # (the model settings below then hold Azure deployment names).
    OPENAI_BASE_URL: str | None = None
    OPENAI_CLASSIFIER_MODEL: str = "gpt-5.4-mini"
    OPENAI_RESPONSE_MODEL: str = "gpt-5.4-mini"
    OPENAI_TIMEOUT_SECONDS: float = 30.0
    # true → no OpenAI calls; fixed placeholder replies so the app can be tested without a key.
    LLM_MOCK: bool = False
    # FR-AIR-013: after this many Echo replies in one conversation, the next reply must close it.
    CONVERSATION_CONTAINMENT_TURNS: int = 30

    class Config:
        env_file = ".env"


settings = Settings()