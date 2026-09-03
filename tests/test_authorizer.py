"""Cognito authorizer allow/deny logic (no network on these branches)."""
import authorizer

METHOD_ARN = 'arn:aws:execute-api:us-east-1:111:api/prod/POST/webhook'


def _effect(event_headers):
    result = authorizer.handler({'methodArn': METHOD_ARN, 'headers': event_headers}, None)
    return result['policyDocument']['Statement'][0]['Effect'], result['principalId']


def test_github_user_agent_exemption_allows():
    effect, _ = _effect({'User-Agent': 'GitHub-Hookshot/abc'})
    assert effect == 'Allow'


def test_github_lowercase_user_agent_allows():
    effect, _ = _effect({'user-agent': 'GitHub-Hookshot/abc'})
    assert effect == 'Allow'


def test_no_token_denies():
    effect, _ = _effect({'User-Agent': 'curl/8'})
    assert effect == 'Deny'


def test_non_bearer_denies():
    effect, _ = _effect({'User-Agent': 'curl/8', 'Authorization': 'Basic abc'})
    assert effect == 'Deny'


def test_malformed_bearer_denies_without_network():
    effect, _ = _effect({'User-Agent': 'curl/8', 'Authorization': 'Bearer not.a.jwt'})
    assert effect == 'Deny'


def test_none_headers_denies():
    effect, _ = _effect(None)
    assert effect == 'Deny'


def test_generate_policy_structure():
    policy = authorizer.generate_policy('user1', 'Allow', METHOD_ARN)
    assert policy['principalId'] == 'user1'
    stmt = policy['policyDocument']['Statement'][0]
    assert stmt['Action'] == 'execute-api:Invoke'
    assert stmt['Effect'] == 'Allow'
    assert stmt['Resource'] == METHOD_ARN
