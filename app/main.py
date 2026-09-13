from fastapi import FastAPI

from app.routers import auth, crisis, health, onboarding

app = FastAPI()

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(crisis.router)