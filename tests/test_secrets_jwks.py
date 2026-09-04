"""Ingest Secrets Manager resolution paths + authorizer JWKS cache behaviour."""
import io
import json
import time

import ingest as mod
import pytest

import authorizer


class FakeSM:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result
        self.error = error

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def clean_cache():
    mod._webhook_secrets.clear()
    yield
    mod._webhook_secrets.clear()


def test_empty_arn_means_no_secret(monkeypatch):
    monkeypatch.setattr(mod, 'WEBSECRET_ARN', '')
    assert mod._get_webhook_secret('github') == ''
    assert mod._verification_enabled('github') is False


def test_secret_fetched_and_cached(monkeypatch):
    fake = FakeSM(result={'SecretString': 'topsecret'})
    monkeypatch.setattr(mod, 'secretsmanager', fake)
    monkeypatch.setattr(mod, 'WEBSECRET_ARN', 'arn:aws:secretsmanager:x:secret:Wh')
    assert mod._get_webhook_secret('github') == 'topsecret'
    assert mod._get_webhook_secret('github') == 'topsecret'
    assert fake.calls == 1


def test_secret_manager_failure_caches_empty(monkeypatch):
    fake = FakeSM(error=RuntimeError('AccessDenied'))
    monkeypatch.setattr(mod, 'secretsmanager', fake)
    monkeypatch.setattr(mod, 'WEBSECRET_ARN', 'arn:bad')
    assert mod._get_webhook_secret('github') == ''
    assert mod._get_webhook_secret('github') == ''
    assert fake.calls == 1


def test_gitlab_uses_its_own_arn(monkeypatch):
    fake = FakeSM(result={'SecretString': 'gl-wh'})
    monkeypatch.setattr(mod, 'secretsmanager', fake)
    monkeypatch.setattr(mod, 'GITLAB_WEBSECRET_ARN', 'arn:gl')
    assert mod._get_webhook_secret('gitlab') == 'gl-wh'


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_jwks_cache_avoids_refetch(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        return FakeResp(json.dumps({'keys': []}).encode())

    monkeypatch.setattr(authorizer.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setitem(authorizer._cache, 'keys', None)
    monkeypatch.setitem(authorizer._cache, 'fetched', 0)
    authorizer._fetch_jwks()
    authorizer._fetch_jwks()
    assert len(calls) == 1


def test_jwks_refetched_after_ttl_expiry(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        return FakeResp(json.dumps({'keys': []}).encode())

    monkeypatch.setattr(authorizer.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setitem(authorizer._cache, 'keys', {'keys': []})
    monkeypatch.setitem(authorizer._cache, 'fetched', time.time() - 400)
    authorizer._fetch_jwks()
    assert len(calls) == 1
