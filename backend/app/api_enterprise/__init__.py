"""Enterprise frontend API adapters.

Two integration paths for the frontends that talk to this framework:

1. **Releases frontend** — direct enterprise routes under /api/enterprise and
   /api/chat, which this framework already serves natively.

2. **Business frontend** — the ``compat`` layer keeps Runtime-owned operations
   local and dispatches native resource contracts to the selected management
   provider. Only explicitly shared operations use spelling/path aliases on OSS;
   absent enterprise capabilities produce FEATURE_UNAVAILABLE, never fake data.
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
