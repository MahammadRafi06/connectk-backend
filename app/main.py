"""ConnectK FastAPI Application Entry Point."""
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import init_db
from app.redis_client import close_redis, get_redis
from app.routers import admin, argocd, auth, clusters, deployments, events, models, nodes
from app.utils.response import error_response

settings = get_settings()

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.LOG_LEVEL)
    )
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ConnectK starting up", env=settings.CONNECTK_ENV)
    await init_db()
    redis = await get_redis()
    await redis.ping()
    logger.info("Database and Redis connected")
    yield
    await close_redis()
    logger.info("ConnectK shutting down")


app = FastAPI(
    title="ConnectK API",
    description="Multi-Cloud AI Infrastructure Management Platform",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    max_age=3600,
)

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/callback", "/api/health", "/"}


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """Double-submit cookie CSRF protection for state-changing requests."""
    if request.method not in CSRF_SAFE_METHODS and request.url.path not in CSRF_EXEMPT_PATHS and not request.url.path.startswith("/api/argocd/"):
        cookie_token = request.cookies.get("connectk_csrf")
        header_token = request.headers.get("X-CSRF-Token")
        if not cookie_token or not header_token or cookie_token != header_token:
            return JSONResponse(
                status_code=403,
                content=error_response(
                    "AUTH_CSRF_INVALID",
                    "Security validation failed. Please refresh and try again.",
                ),
            )

    response = await call_next(request)

    if "connectk_csrf" not in request.cookies:
        csrf_token = secrets.token_urlsafe(32)
        response.set_cookie(
            key="connectk_csrf",
            value=csrf_token,
            httponly=False,
            secure=settings.is_production,
            samesite="lax",
            path="/",
            max_age=settings.SESSION_MAX_AGE_HOURS * 3600,
        )

    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add CSP, HSTS, and other security headers."""
    response = await call_next(request)
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    docs_paths = {"/api/docs", "/api/redoc", "/api/openapi.json"}
    if request.url.path in docs_paths:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    """Assign request ID and log timing."""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)

    logger.info(
        "request",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        elapsed_ms=elapsed_ms,
    )
    return response


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content=error_response("NOT_FOUND", "The requested resource was not found."),
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    logger.error("unhandled_error", exc=str(exc), path=request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_response("DATABASE_ERROR", "An internal error occurred. Please try again."),
    )


# Include routers
app.include_router(auth.router)
app.include_router(clusters.router)
app.include_router(deployments.router)
app.include_router(models.router)
app.include_router(nodes.router)
app.include_router(admin.router)
app.include_router(admin.audit_router)
app.include_router(events.router)
app.include_router(argocd.router)


@app.get("/api/health", tags=["health"])
async def health_check():
    """Public health check endpoint for K8s liveness/readiness probes."""
    return {"status": "ok", "version": "2.0.0", "env": settings.CONNECTK_ENV}


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({"message": "ConnectK API", "docs": "/api/docs"})
