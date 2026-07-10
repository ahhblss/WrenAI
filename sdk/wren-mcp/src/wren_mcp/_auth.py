"""Bearer-token authentication middleware for the streamable-http server.

A remote MCP service must authenticate clients. We use a static bearer token
from the ``WREN_MCP_TOKEN`` env var (or ``--token``). Requests without a
matching ``Authorization: Bearer <token>`` header are rejected with 401
before reaching the MCP app.

stdio transport would skip this (process-local trust), but wren-mcp ships
streamable-http only - every request comes over the network and must be
authenticated. DB/profile secrets are resolved server-side and never returned
to the client regardless of auth outcome.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the expected bearer token."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._expected = token

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        if auth.removeprefix("Bearer ").strip() != self._expected:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)
