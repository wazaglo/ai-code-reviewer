"""GitLab provider token verification + MR context extraction."""
import pytest
from provider import PRContext
from provider.gitlab import GitLabProvider


@pytest.fixture()
def provider():
    return GitLabProvider()


def test_valid_token_accepted(provider):
    assert provider.verify_signature('s3cret', b'{}', {'X-Gitlab-Token': 's3cret'}) is True


def test_lowercase_header_accepted(provider):
    assert provider.verify_signature('s3cret', b'{}', {'x-gitlab-token': 's3cret'}) is True


def test_wrong_token_rejected(provider):
    assert provider.verify_signature('s3cret', b'{}', {'X-Gitlab-Token': 'wrong'}) is False


def test_missing_token_rejected(provider):
    assert provider.verify_signature('s3cret', b'{}', {}) is False


def test_extract_mr_context_open(provider):
    payload = {
        'object_kind': 'merge_request',
        'object_attributes': {
            'action': 'open',
            'iid': 7,
            'title': 'x',
            'last_commit': {'id': 'h'},
            'target_branch': 'main',
            'source_branch': 'feat',
        },
        'project': {'path_with_namespace': 'grp/app'},
    }
    ctx = provider.extract_pr_context(payload)
    assert ctx is not None
    assert ctx.provider == 'gitlab'
    assert ctx.repo == 'grp/app'
    assert ctx.pr_number == 7


def test_extract_mr_context_ignores_non_mr(provider):
    assert provider.extract_pr_context({'object_kind': 'push'}) is None


def test_project_ref_prefers_numeric_id():
    ctx = PRContext(provider='gitlab', repo='g/r', pr_number=1, token='t', project_id='641')
    assert GitLabProvider._project_ref(ctx) == '641'


def test_project_ref_falls_back_to_encoded_path():
    ctx = PRContext(provider='gitlab', repo='g/r', pr_number=1, token='t')
    assert GitLabProvider._project_ref(ctx) == 'g%2Fr'


def test_extract_sets_numeric_project_id(provider):
    payload = {
        'object_kind': 'merge_request',
        'object_attributes': {'action': 'update', 'iid': 3},
        'project': {'path_with_namespace': 'g/r', 'id': 641},
    }
    ctx = provider.extract_pr_context(payload)
    assert ctx.project_id == '641'


def test_extract_ignores_push_events(provider):
    assert provider.extract_pr_context({'object_kind': 'push'}) is None


def test_extract_ignores_merge_action(provider):
    payload = {
        'object_kind': 'merge_request',
        'object_attributes': {'action': 'merge', 'iid': 3},
        'project': {'path_with_namespace': 'g/r', 'id': 1},
    }
    assert provider.extract_pr_context(payload) is None


def test_gl_request_retries_then_succeeds(provider, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

    def fake_request(method, url, **kw):
        calls.append(url)
        return Resp(429 if len(calls) == 1 else 200)

    monkeypatch.setattr('provider.gitlab.requests.request', fake_request)
    monkeypatch.setattr('provider.gitlab.time.sleep', lambda s: None)
    resp = provider._gl_request('GET', 'https://x/y', 'tok')
    assert resp.status_code == 200 and len(calls) == 2


def test_gl_request_gives_up_returns_none(provider, monkeypatch):
    class Resp:
        status_code = 503
        headers = {}

    monkeypatch.setattr('provider.gitlab.requests.request', lambda *a, **k: Resp())
    monkeypatch.setattr('provider.gitlab.time.sleep', lambda s: None)
    assert provider._gl_request('GET', 'https://x/y', 'tok', max_retries=2) is None
