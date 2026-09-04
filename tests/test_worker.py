"""Worker pure functions: comment rendering and repo allowlist."""
from decimal import Decimal

import lambda_function as w


def test_track_cost_uses_decimal(monkeypatch):
    items = []

    class FakeTable:
        def put_item(self, Item):
            items.append(Item)

    monkeypatch.setattr(w, 'COST_TABLE', FakeTable())
    w.track_cost('github', 'a/b', 1, 'm', 10, 20, 1, 5, 0.001234, 2)
    assert len(items) == 1
    assert isinstance(items[0]['cost_usd'], Decimal)


def test_build_comment_clean_review():
    body = w.build_comment([])
    assert 'No issues' in body


def test_build_comment_lists_findings():
    findings = [
        {'file': 'app.py', 'severity': 'high', 'message': 'SQL injection'},
        {'file': 'util.py', 'severity': 'low', 'message': 'naming'},
    ]
    body = w.build_comment(findings)
    assert 'Found 2 issue(s)' in body
    assert 'HIGH' in body and 'LOW' in body
    assert '**app.py**' in body
    assert 'SQL injection' in body
    assert '\U0001F534' in body


def test_build_comment_unknown_severity_defaults_marker():
    body = w.build_comment([{'file': 'x.py', 'severity': 'weird', 'message': 'm'}])
    assert 'WEIRD' in body


def test_allowlisted_empty_allows_all():
    assert w.allowlisted('any/repo') is True


def test_allowlisted_matches_exact_names(monkeypatch):
    monkeypatch.setattr(w, 'REPO_ALLOWLIST', {'wazaglo/app-a', 'org-b/app-b'})
    assert w.allowlisted('wazaglo/app-a') is True
    assert w.allowlisted('wazaglo/app-c') is False


def test_should_merge_or_close_medium_blocks_merge_by_default():
    assert w.should_merge_or_close([{'severity': 'medium'}]) is None


def test_should_merge_or_close_low_only_merges():
    assert w.should_merge_or_close([{'severity': 'low'}, {'severity': 'none'}]) == 'merge'


def test_should_merge_or_close_high_closes():
    assert w.should_merge_or_close([{'severity': 'low'}, {'severity': 'high'}]) == 'close'


def test_should_merge_or_close_empty_merges():
    assert w.should_merge_or_close([]) == 'merge'
