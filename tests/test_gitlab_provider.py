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
