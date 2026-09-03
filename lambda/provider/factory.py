"""Provider factory - detects provider from webhook payload and returns instance."""
import logging
from typing import Any

from . import CodeHostProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

log = logging.getLogger('pr-reviewer.provider.factory')

_PROVIDERS: dict[str, CodeHostProvider] = {
    'github': GitHubProvider(),
    'gitlab': GitLabProvider(),
}


def detect_provider(payload: dict[str, Any], headers: dict[str, str]) -> str | None:
    """Detect provider from webhook payload and headers."""
    # GitHub sends 'X-GitHub-Event' header
    if headers.get('X-GitHub-Event') or headers.get('x-github-event'):
        return 'github'
    # GitLab sends 'X-Gitlab-Event' header
    if headers.get('X-Gitlab-Event') or headers.get('x-gitlab-event'):
        return 'gitlab'
    # Fallback: check payload structure
    if 'repository' in payload and 'pull_request' in str(payload):
        return 'github'
    if 'object_kind' in payload and payload.get('object_kind') == 'merge_request':
        return 'gitlab'
    return None


def get_provider(provider_name: str) -> CodeHostProvider | None:
    """Get provider instance by name."""
    return _PROVIDERS.get(provider_name.lower())


def get_all_providers() -> list[CodeHostProvider]:
    """Get all registered providers."""
    return list(_PROVIDERS.values())


def verify_signature_for_provider(provider_name: str, secret: str, body: bytes, headers: dict[str, str]) -> bool:
    """Verify signature using the appropriate provider's method."""
    provider = get_provider(provider_name)
    if not provider:
        log.warning('unknown_provider_for_verification', extra={'provider': provider_name})
        return False
    return provider.verify_signature(secret, body, headers)