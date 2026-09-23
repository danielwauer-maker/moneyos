from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.migrations import upgrade_database
from app.security.redaction import install_redaction_filters
from app.web.routes import router


@asynccontextmanager
async def lifespan(_: FastAPI):
    upgrade_database()
    install_redaction_filters()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
