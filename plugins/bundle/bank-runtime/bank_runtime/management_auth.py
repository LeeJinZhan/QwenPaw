"""Separate native administrator sessions from authenticated Runtime ingress."""

import re

from fastapi import HTTPException

from .auth import require_service_identity

_SERVICE_ROUTE = re.compile(
    r"/api/bank-runtime/(?:agents/([^/]+)/)?(health|chat|chat/stop)"
)


def authenticate_runtime_request(request) -> bool:
    path = request.url.path
    match = _SERVICE_ROUTE.fullmatch(path)
    if path == "/api/bank-runtime/capabilities":
        if request.method != "GET":
            return False
        path_agent = None
    elif match:
        path_agent, action = match.groups()
        if request.method != ("GET" if action == "health" else "POST"):
            return False
    else:
        return False
    try:
        agent = require_service_identity(
            request.headers.get("authorization"), request.headers.get("x-agent-id")
        )
    except HTTPException:
        return False
    return agent == "bank-assistant" and (path_agent is None or path_agent == agent)


def install_native_auth_bridge(app):
    # AuthMiddleware consults this trusted application hook only after requiring
    # initialized native authentication. Endpoint identity checks still run.
    app.state.require_registered_auth = True
    app.state.service_authenticator = authenticate_runtime_request
    app.state.public_auth_routes = {
        "/api/auth/login": {"POST"},
        "/api/auth/status": {"GET"},
        "/api/version": {"GET"},
        "/api/settings/language": {"GET"},
        "/api/settings/upload-limit": {"GET"},
    }
