"""RDS Postgres connection adapter with IAM DB auth.

Implements the password-as-function / per-connection-token-injection
architecture from `@elationfactory/database-access` (JS), condensed to
the SQLAlchemy idiom. The engine and its pool are created once at
startup; each physical connection opened by the pool receives a fresh
IAM auth token via the `do_connect` event. Tokens are cached with an
8-minute TTL (AWS issues 15-minute tokens).

Usage
-----
    from lavandula.common.db import make_app_engine
    engine = make_app_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

Production SSM keys (flat-hyphenated, under
`/cloud2.lavandulagroup.com/`):

    rds-endpoint, rds-port, rds-database, rds-schema,
    rds-app-user, rds-ro-user
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


_REGION = "us-east-1"
_SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


class IAMTokenManager:
    """Thread-safe cache for RDS IAM DB auth tokens (8-minute TTL).

    The AWS token is valid for 15 minutes. We refresh at 8 minutes to
    leave headroom for long-running connections. The cache is shared
    across threads behind a single lock; the token-generation call is
    fast (local SigV4 signing) so serialising it is acceptable.
    """

    _REFRESH_AFTER_SEC = 8 * 60  # 8 minutes

    def __init__(
        self,
        *,
        region: str,
        host: str,
        port: int,
        user: str,
        rds_client: Any | None = None,
        clock: Any = time.time,
    ) -> None:
        if rds_client is None:
            import boto3
            rds_client = boto3.client("rds", region_name=region)
        self._rds = rds_client
        self._region = region
        self._host = host
        self._port = port
        self._user = user
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expires_at:
                return self._token
            self._token = self._rds.generate_db_auth_token(
                DBHostname=self._host,
                Port=self._port,
                DBUsername=self._user,
                Region=self._region,
            )
            self._expires_at = now + self._REFRESH_AFTER_SEC
            return self._token


def make_engine(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    region: str = _REGION,
    schema: str | None = None,
    pool_size: int = 5,
    max_overflow: int = 10,
    token_manager: IAMTokenManager | None = None,
) -> Engine:
    """Build a SQLAlchemy engine that authenticates via RDS IAM tokens.

    The returned engine holds a single long-lived connection pool. A
    `do_connect` event listener injects a fresh IAM token as the
    password each time the pool opens a new physical connection.
    `pool_pre_ping=True` handles mid-connection token expiry: if a
    pooled connection has been idle long enough for its session to
    drop, the pre-ping fails, the pool discards it, and a new
    connection opens with a current token.

    Parameters
    ----------
    host, port, database, user : connection target.
    region : AWS region for IAM token generation.
    schema : optional Postgres `search_path` applied on connect.
    pool_size, max_overflow : SQLAlchemy pool sizing.
    token_manager : injection point for tests.
    """
    if schema is not None and not _SCHEMA_NAME_RE.match(schema):
        raise ValueError(
            f"invalid schema name {schema!r}: must match "
            f"{_SCHEMA_NAME_RE.pattern}"
        )

    mgr = token_manager or IAMTokenManager(
        region=region, host=host, port=port, user=user,
    )

    url = (
        f"postgresql+psycopg2://{user}@{host}:{port}/{database}"
        f"?sslmode=require"
    )
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=8 * 60,
    )

    @event.listens_for(engine, "do_connect")
    def _inject_iam_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = mgr.token()

    if schema:
        @event.listens_for(engine, "connect")
        def _set_search_path(dbapi_conn, conn_rec):
            cur = dbapi_conn.cursor()
            try:
                cur.execute(f'SET search_path TO "{schema}"')
            finally:
                cur.close()

    return engine


def _engine_from_ssm(user_key: str) -> Engine:
    from .secrets import get_secret
    return make_engine(
        host=get_secret("rds-endpoint"),
        port=int(get_secret("rds-port")),
        database=get_secret("rds-database"),
        user=get_secret(user_key),
        region=_REGION,
        schema=get_secret("rds-schema"),
    )


def make_app_engine() -> Engine:
    """Production engine for the app role (CRUD on `lava_impact`)."""
    return _engine_from_ssm("rds-app-user")


def make_ro_engine() -> Engine:
    """Production engine for the read-only role (SELECT only)."""
    return _engine_from_ssm("rds-ro-user")


__all__ = [
    "IAMTokenManager",
    "make_engine",
    "make_app_engine",
    "make_ro_engine",
]
