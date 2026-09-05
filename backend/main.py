from contextlib import asynccontextmanager
import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import actions, auth_routes, checkpoints, rollback, sessions
from backend.core.cleanup import cleanup_loop
from backend.db.database import health_check, init_db
from backend.config import settings
from backend.db import repositories as repo
from backend.ratelimit import rate_limiter


logging.basicConfig(
    level=os.getenv("CHECKPOINT_LOG_LEVEL", "INFO")
)

logger = logging.getLogger("checkpoint")

_cleanup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Any process restart destroys live runtime handles.
    # Expire those DB rows immediately instead of waiting
    # for the first cleanup interval.
    orphaned = repo.mark_all_running_sessions_expired()

    if orphaned:
        logger.info(
            "Expired %d orphaned session(s) after process startup",
            orphaned,
        )

    global _cleanup_task

    _cleanup_task = asyncio.create_task(cleanup_loop())

    try:
        yield

    finally:
        if _cleanup_task is not None:
            _cleanup_task.cancel()

            try:
                await _cleanup_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="Checkpoint",
    version="0.2.1",
    lifespan=lifespan,
)


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------

# Comma-separated origins for a deployed frontend.
#
# Example:
# CHECKPOINT_CORS_ORIGINS=https://checkpoint.example.com,http://localhost:3000

origins = [
    item.strip()
    for item in os.getenv(
        "CHECKPOINT_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if item.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------

_GLOBAL_RATE_MAX = settings.GLOBAL_RATE_LIMIT_MAX
_GLOBAL_RATE_WINDOW_SEC = settings.GLOBAL_RATE_LIMIT_WINDOW_SEC

_UNLIMITED_PATHS = {
    "/health",
    "/api/health",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers.setdefault(
        "X-Content-Type-Options",
        "nosniff",
    )

    response.headers.setdefault(
        "X-Frame-Options",
        "DENY",
    )

    response.headers.setdefault(
        "Referrer-Policy",
        "no-referrer",
    )

    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )

    if settings.ENVIRONMENT == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

    if (
        request.url.path.startswith("/auth")
        or request.url.path.startswith("/api/auth")
        or request.url.path.startswith("/sessions")
        or request.url.path.startswith("/api/sessions")
        or request.url.path.startswith("/actions")
        or request.url.path.startswith("/api/actions")
    ):
        response.headers["Cache-Control"] = "no-store"

    return response


@app.middleware("http")
async def basic_ip_rate_limit(
    request: Request,
    call_next,
):
    """
    Coarse edge protection against abusive clients.

    This is not a substitute for a real edge such as
    Cloudflare/Render rate limiting, but it stops a single
    misbehaving client from monopolizing the one Uvicorn worker.
    """

    if request.url.path not in _UNLIMITED_PATHS:
        fwd = request.headers.get("x-forwarded-for")

        ip = (
            fwd.split(",")[0].strip()
            if fwd
            else None
        ) or (
            request.client.host
            if request.client
            else "unknown"
        )

        if not rate_limiter.check(
            f"global:{ip}",
            _GLOBAL_RATE_MAX,
            _GLOBAL_RATE_WINDOW_SEC,
        ):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please slow down."
                },
            )

    return await call_next(request)


# ---------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
):
    logger.exception(
        "Unhandled error on %s %s",
        request.method,
        request.url.path,
    )

    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Something went wrong on our end. "
                "Please try again."
            )
        },
    )


# ---------------------------------------------------------
# API routes
# ---------------------------------------------------------

# Expose the stable root API and /api aliases
# for reverse-proxy deployments.

for prefix in ("", "/api"):
    app.include_router(
        auth_routes.router,
        prefix=prefix,
    )

    app.include_router(
        sessions.router,
        prefix=prefix,
    )

    app.include_router(
        actions.router,
        prefix=prefix,
    )

    app.include_router(
        checkpoints.router,
        prefix=prefix,
    )

    app.include_router(
        rollback.router,
        prefix=prefix,
    )


# ---------------------------------------------------------
# CHECKPOINT Web Dashboard
# ---------------------------------------------------------

# The repository contains:
#
# frontend/
#     index.html
#
# We serve that directory at:
#
#     /app/
#
# So:
#
#     https://your-domain.com/app/
#
# loads:
#
#     frontend/index.html
#
# This keeps the dashboard and API on the same origin.

_FRONTEND_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "frontend",
    )
)


if os.path.isdir(_FRONTEND_DIR):
    app.mount(
        "/app",
        StaticFiles(
            directory=_FRONTEND_DIR,
            html=True,
        ),
        name="frontend",
    )

    logger.info(
        "CHECKPOINT frontend mounted at /app from %s",
        _FRONTEND_DIR,
    )

else:
    logger.warning(
        "Frontend directory not found: %s",
        _FRONTEND_DIR,
    )


# ---------------------------------------------------------
# Root endpoint
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "name": "Checkpoint",
        "version": "0.2.1",
        "status": "ok",
        "byok": True,
        "message": (
            "Checkpoint — safety and recovery layer "
            "for AI agent actions"
        ),
    }


# ---------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------

@app.get("/health")
@app.get("/api/health")
def health():
    db_ok = health_check()

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
    }