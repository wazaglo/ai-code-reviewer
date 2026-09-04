"""Worker SQS handler end-to-end with provider/Bedrock/cost mocked."""
import json

import lambda_function as w
import pytest
from provider import PRContext, PRFile

GITHUB_PAYLOAD = {
    'action': 'opened',
    'number': 7,
    'repository': {'full_name': 'a/b'},
    'pull_request': {'number': 7},
}


class FakeProvider:
    def __init__(self):
        self.closed = False
        self.merged = False
        self.comments = []

    def extract_pr_context(self, payload):
        return PRContext(provider='github', repo='a/b', pr_number=payload['number'], token='')

    def get_repo_identifier(self, payload):
        return 'a/b'

    def fetch_pr_files(self, context):
        return [PRFile(filename='x.py', patch='+x = 1', status='added')]

    def post_review_comment(self, context, body):
        self.comments.append(body)
        return True

    def close_pr(self, context):
        self.closed = True
        return True

    def merge_pr(self, context):
        self.merged = True
        return True


@pytest.fixture()
def env(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(w, 'get_provider', lambda name: fake)
    monkeypatch.setattr(w, 'resolve_token', lambda name: 'tok')
    monkeypatch.setattr(w, 'track_cost', lambda **kw: None)
    return fake


def _sqs_event(payload):
    return {'Records': [{'body': json.dumps(payload)}]}


def _run(monkeypatch, fake, findings):
    monkeypatch.setattr(
        w, 'analyze_with_nova',
        lambda f, d: {'findings': findings, 'input_tokens': 1, 'output_tokens': 2, 'processing_time_ms': 3})
    w.handler(_sqs_event(GITHUB_PAYLOAD), None)
    return fake


def test_high_severity_closes_pr(monkeypatch, env):
    fake = _run(monkeypatch, env, [{'severity': 'high', 'message': 'SQLi'}])
    assert fake.closed and not fake.merged
    assert 'Found 1 issue' in fake.comments[0]


def test_clean_pr_merges(monkeypatch, env):
    fake = _run(monkeypatch, env, [])
    assert fake.merged and not fake.closed
    assert 'No issues' in fake.comments[0]


def test_medium_findings_neither_merge_nor_close(monkeypatch, env):
    fake = _run(monkeypatch, env, [{'severity': 'medium', 'message': 'nit'}])
    assert not fake.merged and not fake.closed
    assert len(fake.comments) == 1


def test_skips_unhandled_action(monkeypatch, env):
    monkeypatch.setattr(w, 'analyze_with_nova', lambda f, d: {'findings': []})
    payload = dict(GITHUB_PAYLOAD, action='closed')
    w.handler(_sqs_event(payload), None)
    assert env.comments == [] and not env.closed


def test_bad_message_body_does_not_crash(env):
    w.handler({'Records': [{'body': 'not-json{{'}]}, None)
    assert env.comments == []


def test_unknown_provider_payload_skipped(env):
    w.handler(_sqs_event({'foo': 'bar'}), None)
    assert env.comments == []
