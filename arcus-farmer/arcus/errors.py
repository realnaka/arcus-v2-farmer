"""Error types raised by the Arcus client."""

from __future__ import annotations


class ArcusError(RuntimeError):
    """Base class for API failures."""

    def __init__(self, message: str, *, status: int | None = None, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimited(ArcusError):
    """429 from either layer.  ``retry_after_s`` is what the gateway asked for."""

    def __init__(self, message: str, *, retry_after_s: float, bucket: str = "ip", **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.retry_after_s = retry_after_s
        self.bucket = bucket


class AuthError(ArcusError):
    """401/403 - bad signature, drifted clock, or wrong subaccount scope."""


class OrderRejected(ArcusError):
    """The matching engine refused the order (e.g. ALO would have taken)."""

    def __init__(self, message: str, *, reason: str = "", client_id: str | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.reason = reason
        self.client_id = client_id
