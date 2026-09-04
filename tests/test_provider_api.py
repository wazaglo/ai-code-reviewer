"""Provider REST API methods with the HTTP transport mocked."""
import pytest
from provider import PRContext
from provider.github import GitHubProvider
from provider.gitlab import GitLabProvider


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


def gh_ctx():
    return PRContext(provider='github', repo='a/b', pr_number=5, token='tok', api_base='https://api.github.com')


def gl_ctx():
    return PRContext(provider='gitlab', repo='g/r', pr_number=2, token='tok', api_base='https://gl.example/api/v4', project_id='9')


# ---------------- GitHub ----------------

def test_gh_fetch_maps_code_files_and_skips_docs(monkeypatch):
    p = GitHubProvider()
    payload = [
        {'filename': 'src/app.py', 'patch': '+x', 'status': 'modified'},
        {'filename': 'README.md', 'patch': '+doc', 'status': 'modified'},
        {'filename': 'main.go', 'patch': '', 'status': 'added'},
        {'filename': 'pkg/util.py', 'patch': '+y', 'status': 'added'},
    ]
    monkeypatch.setattr(p, '_gh_request', lambda *a, **k: Resp(200, payload))
    files = p.fetch_pr_files(gh_ctx())
    assert [f.filename for f in files] == ['src/app.py', 'pkg/util.py']
    assert files[1].status == 'added'


def test_gh_fetch_caps_files_per_pr(monkeypatch):
    p = GitHubProvider()
    payload = [{'filename': f'f{i}.py', 'patch': '+x', 'status': 'modified'} for i in range(25)]
    monkeypatch.setattr(p, '_gh_request', lambda *a, **k: Resp(200, payload))
    assert len(p.fetch_pr_files(gh_ctx())) == 10  # MAX_FILES_PER_PR


def test_gh_fetch_raises_on_api_error(monkeypatch):
    p = GitHubProvider()
    monkeypatch.setattr(p, '_gh_request', lambda *a, **k: Resp(401))
    with pytest.raises(RuntimeError):
        p.fetch_pr_files(gh_ctx())


def test_gh_fetch_raises_on_no_response(monkeypatch):
    p = GitHubProvider()
    monkeypatch.setattr(p, '_gh_request', lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        p.fetch_pr_files(gh_ctx())


@pytest.mark.parametrize('status,expected', [(201, True), (200, True), (403, False), (None, False)])
def test_gh_post_comment_status_handling(monkeypatch, status, expected):
    p = GitHubProvider()
    monkeypatch.setattr(p, '_gh_request', lambda *a, **k: Resp(status) if status else None)
    assert p.post_review_comment(gh_ctx(), 'body') is expected


def test_gh_close_sends_closed_state(monkeypatch):
    p = GitHubProvider()
    calls = {}

    def fake(method, url, token, **kw):
        calls.update(method=method, url=url, json=kw.get('json'))
        return Resp(200)

    monkeypatch.setattr(p, '_gh_request', fake)
    assert p.close_pr(gh_ctx()) is True
    assert calls['method'] == 'PATCH' and calls['json'] == {'state': 'closed'}
    assert '/pulls/5' in calls['url']


def test_gh_merge_uses_put(monkeypatch):
    p = GitHubProvider()
    calls = {}

    def fake(method, url, token, **kw):
        calls.update(method=method, url=url)
        return Resp(200)

    monkeypatch.setattr(p, '_gh_request', fake)
    assert p.merge_pr(gh_ctx()) is True
    assert calls['method'] == 'PUT' and calls['url'].endswith('/merge')


# ---------------- GitLab ----------------

def test_gl_fetch_maps_changes_and_statuses(monkeypatch):
    p = GitLabProvider()
    payload = {'changes': [
        {'new_path': 'a.py', 'diff': '+x', 'new_file': True},
        {'new_path': 'docs/readme.md', 'diff': '+d'},
        {'new_path': 'b.py', 'diff': '-y', 'deleted_file': True},
        {'new_path': 'c.py', 'diff': '~z'},
    ]}
    monkeypatch.setattr(p, '_gl_request', lambda *a, **k: Resp(200, payload))
    files = p.fetch_pr_files(gl_ctx())
    assert [(f.filename, f.status) for f in files] == [('a.py', 'added'), ('b.py', 'removed'), ('c.py', 'modified')]


def test_gl_fetch_uses_numeric_project_id_in_url(monkeypatch):
    p = GitLabProvider()
    urls = []
    monkeypatch.setattr(p, '_gl_request', lambda m, url, t, **k: (urls.append(url), Resp(200, {'changes': []}))[1])
    p.fetch_pr_files(gl_ctx())
    assert '/projects/9/merge_requests/2/changes' in urls[0]


def test_gl_fetch_raises_on_failure(monkeypatch):
    p = GitLabProvider()
    monkeypatch.setattr(p, '_gl_request', lambda *a, **k: Resp(404))
    with pytest.raises(RuntimeError):
        p.fetch_pr_files(gl_ctx())


def test_gl_comment_posts_to_notes(monkeypatch):
    p = GitLabProvider()
    calls = {}

    def fake(method, url, token, **kw):
        calls.update(method=method, url=url, body=kw.get('json'))
        return Resp(201)

    monkeypatch.setattr(p, '_gl_request', fake)
    assert p.post_review_comment(gl_ctx(), 'nice') is True
    assert calls['url'].endswith('/notes') and calls['body'] == {'body': 'nice'}


def test_gl_close_sends_state_event(monkeypatch):
    p = GitLabProvider()
    calls = {}

    def fake(method, url, token, **kw):
        calls.update(method=method, json=kw.get('json'))
        return Resp(200)

    monkeypatch.setattr(p, '_gl_request', fake)
    assert p.close_pr(gl_ctx()) is True
    assert calls['method'] == 'PUT' and calls['json'] == {'state_event': 'close'}


def test_gl_merge_returns_false_on_rejected(monkeypatch):
    p = GitLabProvider()
    monkeypatch.setattr(p, '_gl_request', lambda *a, **k: Resp(405))
    assert p.merge_pr(gl_ctx()) is False


def test_gl_get_repo_identifier():
    assert GitLabProvider().get_repo_identifier({'project': {'path_with_namespace': 'g/r'}}) == 'g/r'
