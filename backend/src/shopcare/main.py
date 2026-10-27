from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from shopcare.api.errors import http_error_handler, validation_error_handler
from shopcare.api.health import router as health_router
from shopcare.api.orders import router as orders_router
from shopcare.core.config import Settings
from shopcare.db import build_engine

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = build_engine(settings.database_url)
    app.state.engine = engine

    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.include_router(health_router)
app.include_router(orders_router)
