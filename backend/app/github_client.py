"""Backward-compatible GitHub client imports."""

from app.services.github import add_issue_label, post_issue_comment

__all__ = ["add_issue_label", "post_issue_comment"]
