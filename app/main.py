from fastapi import FastAPI

from app.routers import auth, crisis, health, onboarding, entries, libraries  # sab imports ek sath

app = FastAPI()

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(crisis.router)
app.include_router(entries.router)   # yeh yahan aana chahiye, baaki routers ke sath
app.include_router(libraries.router)
