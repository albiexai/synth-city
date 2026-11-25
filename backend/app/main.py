"""FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy.exc import OperationalError
from mcp_shim.environment_warning import warn_legacy_environment

from app.api.v1.router import router as api_v1_router
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.services.agent_bridge.attachments import agent_bridge_attachment_service
from app.services.github_dispatch_scheduler import github_dispatch_scheduler
from app.services.github_rate_limit_history import GithubRateLimitHistory
from app.services.github_rate_limit_service import github_rate_limit_service
from app.services.operations_health_service import operations_health_service
from app.services.test_instance_campaign_service import test_instance_campaign_poller

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    warn_legacy_environment()
    # Startup: Initialize database
    await init_db()
    # Provider recovery needs the migrated database and retargeted backup index.
    # It must finish its attempt before cleanup or autonomous services start.
    from app.services.provider_upgrade import upgrade_providers
    await upgrade_providers()
    try:
        async with AsyncSessionLocal() as db:
            await agent_bridge_attachment_service.cleanup_expired(db)
    except Exception:
        logger.exception("Failed to clean up expired Agent Bridge attachments")
    history = GithubRateLimitHistory(AsyncSessionLocal)
    previous_observer = github_rate_limit_service.window_observer
    github_rate_limit_service.window_observer = history.observe
    try:
        if settings.test_instance:
            # A test instance runs no autonomous dispatch and must not signal
            # tmux clients that belong to other servers on the host.
            logger.warning("Test instance: dispatch scheduler and relay cleanup are off")
            await test_instance_campaign_poller.start()
        else:
            try:
                await github_dispatch_scheduler.start()
            except Exception:
                logger.exception("Failed to start autonomous GitHub dispatch scheduler")
            # Clean up any orphaned relay processes from previous runs
            from app.services.cc_bridge.pty_relay import cleanup_orphaned_relays
            cleanup_orphaned_relays()
        yield
        from app.services.cc_bridge.pty_relay import close_all_relays
        if settings.test_instance:
            await test_instance_campaign_poller.shutdown()
        await github_dispatch_scheduler.shutdown()
        await close_all_relays()
    finally:
        github_rate_limit_service.window_observer = previous_observer
        await history.drain()


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=settings.cors_methods,
    allow_headers=settings.cors_headers,
)

# Include API routers
app.include_router(api_v1_router, prefix=settings.api_v1_prefix)


@app.get("/health")
async def health():
    """Health check endpoint."""
    body = {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running"
    }
    # 'status' keeps its value: deploy tooling and the E2E harness read it, and this body keeps its exact shape
    # while nothing is wrong. While #941 findings are open, or cannot be read, it adds 'health': 'degraded', the
    # reason codes ('findings_unreadable' when the store fails) and the count.
    findings = await operations_health_service.health()
    if findings["reasons"]:
        body.update(findings)
    if settings.test_instance:
        # The E2E harness checks that this server is its own run's instance.
        body["test_instance"] = {
            "root": settings.test_instance_root,
            "token": settings.test_instance_token,
        }
    return body

# Serve static files from the frontend build directory
frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend", "dist")


HASHED_ASSET_CACHE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.lstrip("/").startswith("assets/"):
            response.headers["Cache-Control"] = HASHED_ASSET_CACHE
        else:
            response.headers["Cache-Control"] = REVALIDATE
        return response


async def database_busy_exception_handler(request, exc):
    """#1149: a lock that outlasts the busy timeout is retryable, so answer 503 instead of 500."""
    from app.services.sqlite_busy_retry import is_sqlite_busy

    # Every busy or locked code, extended ones included (262, a shared-cache table lock), not only the message text.
    if is_sqlite_busy(exc):
        logger.warning("database_busy path=%s", request.url.path)
        return JSONResponse({"detail": "database_busy"}, status_code=503, headers={"Retry-After": "1"})
    logger.error("Unhandled database error on %s", request.url.path, exc_info=exc)
    return PlainTextResponse("Internal Server Error", status_code=500)


def install_database_busy_handler(target: FastAPI) -> None:
    target.add_exception_handler(OperationalError, database_busy_exception_handler)


install_database_busy_handler(app)


async def spa_not_found_exception_handler(request, exc):
    """Serve the SPA for UI routes without erasing API error details or masking missing assets."""
    if request.url.path.startswith((settings.api_v1_prefix, "/assets/")):
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return FileResponse(
        os.path.join(frontend_path, "index.html"),
        status_code=200,
        headers={"Cache-Control": REVALIDATE},
    )


if os.path.exists(frontend_path):
    app.mount("/", FrontendFiles(directory=frontend_path, html=True), name="frontend")
    app.add_exception_handler(404, spa_not_found_exception_handler)
else:
    @app.get("/")
    async def root():
        """Root endpoint fallback when frontend is not built."""
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "message": "Frontend not found. Please build the frontend."
        }
