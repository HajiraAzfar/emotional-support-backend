from fastapi import FastAPI

from app.routers import health
from app.routers import auth, health, onboarding

app = FastAPI()

app.include_router(health.router)

from app.routers import auth, health
app.include_router(onboarding.router)
app.include_router(auth.router)