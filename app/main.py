import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import auth, crisis, exposure, health, onboarding, entries, libraries, insights, learning, savoring, thought, voice

# App loggers at INFO (skill, triage and card per reply); libraries stay at WARNING.
logging.basicConfig(level=logging.WARNING)
logging.getLogger("app").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # RAG: embed the knowledge cards in the background, so the first chat turn doesn't pay for it.
    threading.Thread(target=entries.warm_rag, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(crisis.router)
app.include_router(entries.router)   # yeh yahan aana chahiye, baaki routers ke sath
app.include_router(libraries.router)
app.include_router(insights.router)
app.include_router(learning.router)
app.include_router(savoring.router)
app.include_router(thought.router)
app.include_router(exposure.router)
app.include_router(voice.router)
