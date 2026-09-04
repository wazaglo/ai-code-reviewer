"""Ingest handler: auth matrix, signature verification, SQS enqueue (AWS mocked)."""
import base64
import hashlib
import hmac
import json

import ingest as mod
import pytest


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append({'QueueUrl': QueueUrl, 'MessageBody': MessageBody})


@pytest.fixture()
def ing(monkeypatch):
    fake = FakeSQS()
    monkeypatch.setattr(mod, 'sqs', fake)
    monkeypatch.setitem(mod._webhook_secrets, 'github', 's3cret')
    monkeypatch.delenv('WEBHOOK_USER', raising=False)
    monkeypatch.delenv('WEBHOOK_PASS', raising=False)
    return mod, fake


def _event(body_str, headers, b64=False):
    return {
        'body': base64.b64encode(body_str.encode()).decode() if b64 else body_str,
        'headers': headers,
        'isBase64Encoded': b64,
    }


def _gh_headers(body_str, secret='s3cret', valid=True):
    digest = hmac.new(secret.encode(), body_str.encode(), hashlib.sha256).hexdigest()
    if not valid:
        digest = 'deadbeef' * 8
    return {
        'X-GitHub-Event': 'pull_request',
        'X-Hub-Signature-256': f'sha256={digest}',
        'User-Agent': 'GitHub HOOK-DELIVER',
    }


def test_valid_github_webhook_enqueues(ing):
    mod, fake = ing
    body = json.dumps({'action': 'opened'})
    resp = mod.handler(_event(body, _gh_headers(body)), None)
    assert resp['statusCode'] == 200
    assert 'queued' in resp['body'].lower()
    assert len(fake.sent) == 1
    assert json.loads(fake.sent[0]['MessageBody'])['action'] == 'opened'


def test_invalid_signature_rejected(ing):
    mod, fake = ing
    body = json.dumps({'action': 'opened'})
    resp = mod.handler(_event(body, _gh_headers(body, valid=False)), None)
    assert resp['statusCode'] == 401
    assert fake.sent == []


def test_wrong_secret_rejected(ing):
    mod, fake = ing
    body = json.dumps({'action': 'opened'})
    resp = mod.handler(_event(body, _gh_headers(body, secret='other')), None)
    assert resp['statusCode'] == 401
    assert fake.sent == []


def test_unknown_provider_rejected(ing):
    mod, fake = ing
    resp = mod.handler(_event('{}', {'User-Agent': 'curl/8'}), None)
    assert resp['statusCode'] == 400
    assert fake.sent == []


def test_base64_body_decoded(ing):
    mod, fake = ing
    body = json.dumps({'action': 'synchronize'})
    resp = mod.handler(_event(body, _gh_headers(body), b64=True), None)
    assert resp['statusCode'] == 200
    assert len(fake.sent) == 1


def test_unparseable_body_still_forwarded_when_signature_valid(ing):
    mod, fake = ing
    body = 'not-json{{'
    resp = mod.handler(_event(body, _gh_headers(body)), None)
    assert resp['statusCode'] == 200
    assert len(fake.sent) == 1


def test_basic_auth_required_for_api_users(ing, monkeypatch):
    mod, fake = ing
    monkeypatch.setenv('WEBHOOK_USER', 'ci')
    monkeypatch.setenv('WEBHOOK_PASS', 'pw')
    resp = mod.handler(_event('{}', {'User-Agent': 'curl/8'}), None)
    assert resp['statusCode'] == 401
    assert 'Unauthorized' in resp['body']

    good = base64.b64encode(b'ci:pw').decode()
    body = json.dumps({'action': 'opened'})
    headers = _gh_headers(body)
    headers['Authorization'] = f'Basic {good}'
    resp = mod.handler(_event(body, headers), None)
    assert resp['statusCode'] == 200


def test_github_user_agent_exempt_from_basic_auth(ing, monkeypatch):
    mod, fake = ing
    monkeypatch.setenv('WEBHOOK_USER', 'ci')
    monkeypatch.setenv('WEBHOOK_PASS', 'pw')
    body = json.dumps({'action': 'opened'})
    resp = mod.handler(_event(body, _gh_headers(body)), None)
    assert resp['statusCode'] == 200
    assert len(fake.sent) == 1


def test_gitlab_form_body_normalized_to_json(ing, monkeypatch):
    mod, fake = ing
    monkeypatch.setitem(mod._webhook_secrets, 'gitlab', 'glsec')
    form = 'object_kind=merge_request&object_attributes%5Biid%5D=7&object_attributes%5Baction%5D=open&project%5Bpath_with_namespace%5D=g%2Fr'
    resp = mod.handler(_event(form, {
        'X-Gitlab-Event': 'Merge Request Hook',
        'X-Gitlab-Token': 'glsec',
        'User-Agent': 'GitLab',
    }), None)
    assert resp['statusCode'] == 200
    msg = json.loads(fake.sent[0]['MessageBody'])
    assert msg['object_kind'] == 'merge_request'
    assert msg['object_attributes']['iid'] == '7'
    assert msg['project']['path_with_namespace'] == 'g/r'
