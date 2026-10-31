from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine

from app.api.errors import ApiProblem
from app.config import Settings
from app.security.dev_tokens import DevToken, authenticate_dev_token
from app.security.principal import AuthenticationError, Principal, authenticate

UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Bearer realm="chi"'}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        raise _unauthorized("A bearer token is required.")
    try:
        if settings.auth_mode == "dev_tokens":
            dev_tokens: tuple[DevToken, ...] = request.app.state.dev_tokens
            principal = authenticate_dev_token(dev_tokens, token.strip())
        else:
            principal = authenticate(
                engine, settings.api_key_pepper.get_secret_value(), token.strip()
            )
    except AuthenticationError as exc:
        raise _unauthorized("The token is not valid.") from exc
    if principal.role == "readonly" and request.method not in SAFE_METHODS:
        raise ApiProblem(403, "forbidden", "Forbidden", "A readonly token cannot change data.")
    return principal


def _unauthorized(detail: str) -> ApiProblem:
    return ApiProblem(401, "unauthorized", "Unauthorized", detail, UNAUTHORIZED_HEADERS)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
