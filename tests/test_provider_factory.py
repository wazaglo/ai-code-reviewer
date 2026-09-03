"""Provider detection and dispatch."""
from provider.factory import (
    detect_provider,
    get_provider,
    verify_signature_for_provider,
)


def test_detect_github_by_header():
    assert detect_provider({}, {'X-GitHub-Event': 'pull_request'}) == 'github'
    assert detect_provider({}, {'x-github-event': 'pull_request'}) == 'github'


def test_detect_gitlab_by_header():
    assert detect_provider({}, {'X-Gitlab-Event': 'Merge Request Hook'}) == 'gitlab'


def test_detect_github_by_payload_fallback():
    payload = {'repository': {'full_name': 'a/b'}, 'pull_request': {'number': 1}}
    assert detect_provider(payload, {}) == 'github'


def test_detect_gitlab_by_payload_fallback():
    assert detect_provider({'object_kind': 'merge_request'}, {}) == 'gitlab'


def test_detect_unknown_returns_none():
    assert detect_provider({'foo': 'bar'}, {}) is None


def test_get_provider_unknown():
    assert get_provider('bitbucket') is None


def test_dispatch_rejects_bad_signature():
    assert verify_signature_for_provider('github', 'secret', b'{}', {}) is False
