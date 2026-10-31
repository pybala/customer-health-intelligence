import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import ApiProblem, handle_problem
from app.api.routes import health, v1
from app.config import Settings
from app.db.session import make_engine, missing_organizations
from app.logging_setup import configure_logging
from app.security.dev_tokens import load_dev_tokens

logger = logging.getLogger(__name__)

DEV_AUTH_WARNING = (
    "AUTH_MODE=dev_tokens is active: development tokens are plaintext, unhashed, "
    "not individually revocable, and NOT PRODUCTION-READY. Use for the local demo only."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()  # type: ignore[call-arg]
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved.auth_mode == "dev_tokens":
            tokens = load_dev_tokens(resolved.dev_tokens_file)
            unknown = missing_organizations(app.state.engine, {tok.org_id for tok in tokens})
            if unknown:
                raise RuntimeError(
                    f"Dev token file references organizations that do not exist: {unknown}"
                )
            app.state.dev_tokens = tokens
            logger.warning(DEV_AUTH_WARNING, extra={"fields": {"tokens": len(tokens)}})
        yield
        app.state.engine.dispose()

    app = FastAPI(
        title="Customer Health Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.engine = make_engine(resolved.database_url)
    app.add_exception_handler(ApiProblem, handle_problem)
    app.include_router(health.router)
    app.include_router(v1.router)
    return app
