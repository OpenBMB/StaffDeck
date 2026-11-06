"""Enterprise frontend API adapters.

Two integration paths for the frontends that talk to this framework:

1. **Releases frontend** — direct enterprise routes under /api/enterprise and
   /api/chat, which this framework already serves natively.

2. **Business frontend** — a transparent ASGI layer (``compat``) that lets the
   business SPA (staffdeck_business/staffdeck-frontend) address the framework via
   its split-service prefixes (/api/agent-control-plane, /api/agent-platform,
   /api/agent-sops, /api/agent-tools, /api/skills, /api/knowledge/v1, /api/admin,
   /api/organization) without a second set of handlers. It rewrites paths, fills in
   the ``tenant_id`` the business SPA never sends, and projects the handful of
   request/response shapes that differ.
"""

from fastapi import FastAPI

__all__ = ["register_business_compat"]


def register_business_compat(app: FastAPI) -> None:
    """Install the business-frontend compatibility middleware.

    Call after every OSS router is registered so the middleware wraps the whole
    route tree. Requests that do not match a business prefix pass through untouched.
    """
    from app.api_enterprise.compat import BusinessContractMiddleware

    app.add_middleware(BusinessContractMiddleware)
