"""GitHub provider signature verification + PR context extraction."""
import hashlib
import hmac

import pytest
from provider.github import GitHubProvider


@pytest.fixture()
def provider():
    return GitHubProvider()


def _sign(secret, body):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted(provider):
    body = b'{"action":"opened"}'
    sig = _sign('s3cret', body)
    assert provider.verify_signature('s3cret', body, {'X-Hub-Signature-256': sig}) is True


def test_lowercase_header_accepted(provider):
    body = b'{"action":"opened"}'
    sig = _sign('s3cret', body)
    assert provider.verify_signature('s3cret', body, {'x-hub-signature-256': sig}) is True


def test_tampered_body_rejected(provider):
    sig = _sign('s3cret', b'{"action":"opened"}')
    assert provider.verify_signature('s3cret', b'{"action":"closed"}', {'X-Hub-Signature-256': sig}) is False


def test_wrong_secret_rejected(provider):
    body = b'{"action":"opened"}'
    sig = _sign('other', body)
    assert provider.verify_signature('s3cret', body, {'X-Hub-Signature-256': sig}) is False


def test_missing_signature_rejected(provider):
    assert provider.verify_signature('s3cret', b'{}', {}) is False


def test_extract_pr_context_opened(provider):
    payload = {
        'action': 'opened',
        'number': 42,
        'repository': {'full_name': 'acme/web', 'clone_url': 'https://github.com/acme/web.git'},
        'pull_request': {'title': 'x', 'head': {'ref': 'b', 'sha': 'h'}, 'base': {'sha': 'b0'}},
    }
    ctx = provider.extract_pr_context(payload)
    assert ctx is not None
    assert ctx.provider == 'github'
    assert ctx.repo == 'acme/web'
    assert ctx.pr_number == 42


def test_extract_pr_context_ignores_other_actions(provider):
    assert provider.extract_pr_context({'action': 'closed', 'number': 1}) is None


def test_draft_pr_returns_none(provider):
    payload = {
        'action': 'opened',
        'number': 2,
        'repository': {'full_name': 'a/b'},
        'pull_request': {'draft': True},
    }
    assert provider.extract_pr_context(payload) is None


def test_gh_request_retries_rate_limit(provider, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {'Retry-After': '0'}

    def fake_request(method, url, **kw):
        calls.append(url)
        return Resp(403 if len(calls) < 3 else 200)

    monkeypatch.setattr('provider.github.requests.request', fake_request)
    monkeypatch.setattr('provider.github.time.sleep', lambda s: None)
    resp = provider._gh_request('GET', 'https://x/y', 'tok')
    assert resp.status_code == 200 and len(calls) == 3
