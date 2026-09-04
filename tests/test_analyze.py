"""analyze_with_nova with a fake Bedrock client (no AWS calls)."""

import lambda_function as w
import pytest


class FakeBedrock:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.last_prompt = None

    def converse(self, modelId, messages, inferenceConfig):
        self.last_prompt = messages[0]['content'][0]['text']
        if self.error:
            raise self.error
        return {
            'output': {'message': {'content': [{'text': self.text}]}},
            'usage': {'inputTokens': 11, 'outputTokens': 22},
        }


@pytest.fixture()
def fake(monkeypatch):
    f = FakeBedrock(text='[]')
    monkeypatch.setattr(w, 'bedrock', f)
    return f


def test_clean_code_no_findings(fake):
    fake.text = 'Here you go:\n```json\n[]\n```'
    r = w.analyze_with_nova('a.py', '+x = 1')
    assert r['findings'] == []
    assert r['input_tokens'] == 11 and r['output_tokens'] == 22


def test_findings_parsed_from_prose(fake):
    fake.text = 'Findings: [{"severity":"high","message":"SQLi"}] done.'
    r = w.analyze_with_nova('a.py', '+q = "SELECT " + x')
    assert r['findings'] == [{'severity': 'high', 'message': 'SQLi'}]


def test_no_json_array_means_no_findings(fake):
    fake.text = 'I could not analyze this diff.'
    assert w.analyze_with_nova('a.py', '+x = 1')['findings'] == []


def test_long_diff_is_truncated(fake):
    w.analyze_with_nova('a.py', 'x' * (w.MAX_DIFF_CHARS_PER_FILE + 500))
    assert 'diff truncated' in fake.last_prompt
    assert len(fake.last_prompt) < w.MAX_DIFF_CHARS_PER_FILE + 2000


def test_bedrock_failure_returns_error_not_exception(fake):
    fake.error = RuntimeError('ThrottlingException')
    r = w.analyze_with_nova('a.py', '+x = 1')
    assert r['findings'] == []
    assert 'ThrottlingException' in r['error']


def test_malformed_json_returns_error_safely(fake):
    fake.text = '[{"severity": "high", }]'
    r = w.analyze_with_nova('a.py', '+x = 1')
    assert r['findings'] == []
    assert 'error' in r
