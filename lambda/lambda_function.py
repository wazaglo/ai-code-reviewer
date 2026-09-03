"""PR review worker - consumes queued PR events and runs an AI review.

Pipeline: SQS -> GitHub REST API (changed files + patches) -> Bedrock (Nova)
analysis -> post findings back to GitHub as a PR comment.

Design notes:
  * No git binary: the Lambda runtime does not ship one. Changed files and
    their diffs come straight from the GitHub REST API, which is cheaper and
    more reliable in Lambda than cloning.
  * GitHub token from AWS Secrets Manager (preferred) or GITHUB_TOKEN env var.
  * Dead-letter-aware: raise on failure so SQS retries and eventually moves
    the message to the DLQ (maxReceiveCount). Do NOT delete messages by hand -
    the Lambda event source mapping owns message deletion.
  * Optional repository allowlist to restrict which repos are analyzed.
  * Structured JSON logging, retry with backoff on the GitHub API.
"""
import json
import logging
import os
import time

import boto3
import requests

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)
secretsmanager = boto3.client('secretsmanager', region_name=AWS_REGION)

BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0')
GITHUB_TOKEN_ARN = os.environ.get('GITHUB_TOKEN_ARN', '')

# Comma-separated allowlist, e.g. "acme/web,acme/app". Empty = allow all.
REPO_ALLOWLIST = {r.strip() for r in os.environ.get('REPO_ALLOWLIST', '').split(',') if r.strip()}

CODE_EXTENSIONS = {'.py', '.js', '.ts', '.java', '.go', '.rb', '.php', '.c', '.cpp', '.h', '.cs', '.rs'}
MAX_FILES_PER_PR = 10
MAX_DIFF_CHARS_PER_FILE = 6000

GITHUB_API = 'https://api.github.com'

log = logging.getLogger('pr-reviewer')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(_h)
log.setLevel(logging.INFO)

_token_cache = None


def resolve_github_token():
    """Secrets Manager first, then fall back to the GITHUB_TOKEN env var."""
    global _token_cache
    if _token_cache is not None:
        return _token_cache
    if GITHUB_TOKEN_ARN:
        resp = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_ARN)
        _token_cache = resp['SecretString']
    else:
        _token_cache = os.environ.get('GITHUB_TOKEN') or ''
    return _token_cache


def allowlisted(repo_name):
    return not REPO_ALLOWLIST or repo_name in REPO_ALLOWLIST


def _gh_request(method, url, token, max_retries=4, **kwargs):
    """GitHub API request with exponential backoff on 429/5xx."""
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    resp = None
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code in (403, 429, 500, 502, 503, 504):
            retry_after = int(resp.headers.get('Retry-After', 0) or 0)
            wait = retry_after or (2 ** attempt)
            log.info('github_retry', extra={'url': url, 'status': resp.status_code, 'wait': wait})
            time.sleep(min(wait, 30))
            continue
        return resp
    return resp


def fetch_pr_files(repo, pr_number, token):
    """Changed files for a PR via REST. Each item has filename/status/patch."""
    url = f'{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files?per_page=100'
    resp = _gh_request('GET', url, token)
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f'GitHub API files failed: {resp.status_code if resp is not None else "no response"} {resp.text[:200] if resp is not None else ""}')
    return resp.json()


def analyze_with_nova(filepath, diff_text):
    if len(diff_text) > MAX_DIFF_CHARS_PER_FILE:
        diff_text = diff_text[:MAX_DIFF_CHARS_PER_FILE] + '\n... (diff truncated)'

    prompt = f"""You are an expert code reviewer analyzing the DIFF of a pull request.

File: {filepath}

Diff (+ added / - removed):

{diff_text}

Analyze the CHANGED code for:
1. **Security vulnerabilities** - SQL injection, hardcoded secrets, command/code injection, disabled TLS verification, XSS
2. **Code quality** - readability, maintainability, error handling, best practices
3. **Performance issues** - inefficient loops, memory leaks, slow operations

Return your findings as a JSON array with this exact structure and nothing else:
[
  {{
    "severity": "high",
    "category": "security",
    "message": "Hardcoded API key detected"
  }}
]

Use severity values: "high", "medium", "low".
If no issues found, return: []"""

    try:
        response = bedrock.converse(
            modelId=BEDROCK_MODEL,
            messages=[{'role': 'user', 'content': [{'text': prompt}]}],
            inferenceConfig={'maxTokens': 2000, 'temperature': 0.1},
        )
        text = response['output']['message']['content'][0]['text']
        start, end = text.find('['), text.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return []
    except Exception as e:  # noqa: BLE001
        log.error('bedrock_error', extra={'error': str(e)[:500], 'model': BEDROCK_MODEL})
        raise


def build_comment(findings):
    if not findings:
        return '\u2705 **AI Review Complete**\n\nNo issues detected in this PR!'
    emoji = {'high': '\U0001F534', 'medium': '\U0001F7E1', 'low': '\U0001F535'}
    lines = []
    for f in findings:
        sev = (f.get('severity') or 'medium').lower()
        marker = emoji.get(sev, '\U0001F7E1')
        file_part = f"**{f['file']}** - " if f.get('file') else ''
        lines.append(f"{marker} **{sev.upper()}** {file_part}{f.get('message', '')}")
    return (
        f'## \U0001F916 AI Code Review\n\nFound {len(findings)} issue(s):\n\n'
        + '\n'.join(lines)
        + '\n\n---\n*Generated by Amazon Nova AI*'
    )


def post_github_review(repo, pr_number, findings, token):
    body = build_comment(findings)
    url = f'{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments'
    resp = _gh_request('POST', url, token, json={'body': body})
    if resp is None or resp.status_code not in (200, 201):
        status = resp.status_code if resp is not None else 'none'
        text = resp.text[:200] if resp is not None else ''
        raise RuntimeError(f'comment post failed: {status} {text}')
    log.info('review_posted', extra={'status': resp.status_code, 'pr': pr_number})


def handler(event, context):
    log.info('sqs_event', extra={'records': len(event.get('Records', []))})

    for record in event.get('Records', []):
        try:
            payload = json.loads(record['body'])
        except (ValueError, KeyError) as e:
            log.error('bad_message_body', extra={'error': str(e), 'body': record.get('body', '')[:200]})
            continue  # unparseable poison: drop it, do not spin the queue

        action = payload.get('action')
        pr_number = payload.get('number')
        repo_name = payload.get('repository', {}).get('full_name')

        if action not in ('opened', 'synchronize', 'reopened'):
            log.info('skip_action', extra={'action': action})
            continue

        if not repo_name or not allowlisted(repo_name) or not pr_number:
            log.info('skip_repo', extra={'repo': repo_name})
            continue

        token = resolve_github_token()
        log.info('processing_pr', extra={'pr': pr_number, 'repo': repo_name})

        changed = fetch_pr_files(repo_name, pr_number, token)
        code_files = [
            f for f in changed
            if os.path.splitext(f.get('filename', ''))[1] in CODE_EXTENSIONS and f.get('patch')
        ][:MAX_FILES_PER_PR]
        log.info('files_to_analyze', extra={'count': len(code_files), 'pr': pr_number})

        all_findings = []
        for entry in code_files:
            filepath = entry['filename']
            log.info('analyzing_file', extra={'file': filepath, 'pr': pr_number})
            findings = analyze_with_nova(filepath, entry['patch'])
            for f in findings:
                f['file'] = filepath
            all_findings.extend(findings)

        if token:
            post_github_review(repo_name, pr_number, all_findings, token)
        else:
            log.info('dry_run_findings', extra={'findings': json.dumps(all_findings)[:2000]})

        log.info('pr_processed', extra={'pr': pr_number, 'findings': len(all_findings)})

    return {'statusCode': 200, 'body': 'Processing complete'}
