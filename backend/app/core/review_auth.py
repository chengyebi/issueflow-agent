import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

REVIEW_ADMIN_HEADER_NAME = "X-Review-Admin-Token"

review_admin_header = APIKeyHeader(
    name=REVIEW_ADMIN_HEADER_NAME,
    scheme_name="ReviewAdminToken",
    description="Administrative token required to read and decide review tasks.",
    auto_error=False,
)


def require_review_admin(
    provided_token: Annotated[str | None, Security(review_admin_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    configured_token = settings.review_admin_token
    expected_token = (
        configured_token.get_secret_value() if configured_token is not None else ""
    )

    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Review admin authentication is not configured",
        )

    provided_bytes = provided_token.encode("utf-8") if provided_token else b""
    expected_bytes = expected_token.encode("utf-8")

    if not secrets.compare_digest(provided_bytes, expected_bytes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid review admin token",
            headers={"WWW-Authenticate": "APIKey"},
        )
