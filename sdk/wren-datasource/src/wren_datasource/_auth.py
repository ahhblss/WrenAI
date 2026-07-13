"""Bearer-token authentication middleware for the datasource REST service.

Mirrors wren-mcp's ``_auth.py``: a remote service must authenticate clients.
The datasource service returns expanded connection_info (real DB credentials),
so the token gate is **mandatory** - never deploy it on a network without a
token. DB/profile secrets are resolved server-side and only leave the service
toward an authenticated wren-mcp over a trusted network (same-host unix socket
or mTLS).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the expected bearer token."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._expected = token

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        if auth.removeprefix("Bearer ").strip() != self._expected:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)
