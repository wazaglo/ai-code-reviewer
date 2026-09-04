"""Provider abstraction for code hosting platforms (GitHub, GitLab, etc.)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PRFile:
    """Represents a changed file in a PR/MR."""
    filename: str
    patch: str
    status: str


@dataclass
class PRContext:
    """Context identifying a pull/merge request."""
    provider: str
    repo: str
    pr_number: int
    token: str
    api_base: str = ""
    project_id: str = ""


class CodeHostProvider(ABC):
    """Abstract base class for code hosting providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'github', 'gitlab')."""
        pass

    @property
    @abstractmethod
    def webhook_event_types(self) -> list[str]:
        """Webhook event types that trigger a review."""
        pass

    @abstractmethod
    def verify_signature(self, secret: str, body: bytes, headers: dict[str, str]) -> bool:
        """Verify webhook signature. Return True if valid."""
        pass

    @abstractmethod
    def extract_pr_context(self, payload: dict[str, Any]) -> PRContext | None:
        """Extract PR/MR context from webhook payload. Returns None if not a reviewable event."""
        pass

    @abstractmethod
    def fetch_pr_files(self, context: PRContext) -> list[PRFile]:
        """Fetch changed files for the PR/MR."""
        pass

    @abstractmethod
    def post_review_comment(self, context: PRContext, body: str) -> bool:
        """Post review comment to the PR/MR. Return True on success."""
        pass

    @abstractmethod
    def merge_pr(self, context: PRContext) -> bool:
        """Merge the PR/MR. Return True on success."""
        pass

    @abstractmethod
    def close_pr(self, context: PRContext) -> bool:
        """Close the PR/MR. Return True on success."""
        pass

    @abstractmethod
    def get_repo_identifier(self, payload: dict[str, Any]) -> str:
        """Get repo identifier for allowlist checking (e.g., 'org/repo')."""
        pass
