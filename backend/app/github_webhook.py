"""Backward-compatible webhook security import."""

from app.core.security import verify_github_signature

__all__ = ["verify_github_signature"]

