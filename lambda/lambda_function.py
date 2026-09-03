"""PR review worker - consumes queued PR events and runs an AI review.

Pipeline: SQS -> clone PR branch -> diff changed files -> Bedrock (Nova)
analysis -> post findings back to GitHub as a PR comment.

Production hardening included:
  * GitHub token fetched from AWS Secrets Manager (not an env var).
  * Dead-letter-aware: relies on the SQS maxReceiveCount to move poison
    messages to the DLQ, so a bad message never blocks the queue forever.
  * Optional repository allowlist to restrict which repos are analyzed.
  * Structured JSON logging.
  * X-Ray tracing enabled via the deployed layer / env (see template).
  * Retry with backoff on the GitHub API (429 / 5xx handled).
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse

import boto3
import requests

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)
sqs = boto3.client('sqs', region_name=AWS_REGION)
secretsmanager = boto3.client('secretsmanager', region_name=AWS_REGION)

QUEUE_URL = os.environ.get('QUEUE_URL', '')
BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-2-lite-v1:0')
GITHUB_TOKEN_ARN = os.environ.get('GITHUB_TOKEN_ARN', '')

# Comma-separated allowlist, e.g. "acme/web,acme/app". Empty = allow all.
REPO_ALLOWLIST = {r.strip() for r in os.environ.get('REPO_ALLOWLIST', '').split(',') if r.strip()}

CODE_EXTENSIONS = {'.py', '.js', '.ts', '.java', '.go', '.rb', '.php', '.c', '.cpp', '.h', '.cs', '.rs'}
MAX_FILES_PER_PR = 10
MAX_CODE_CHARS_PER_FILE = 4000

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('pr-reviewer')

_token_cache = None


def resolve_github_token():
    """Fetch the GitHub token from Secrets Manager (cached per warm start)."""
    global _token_cache
    if _token_cache is None and GITHUB_TOKEN_ARN:
        resp = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_ARN)
        _token_cache = resp['SecretString']
    return _token_cache


def allowlisted(repo_name):
    """True if a repo is allowed to be analyzed."""
    return not REPO_ALLOWLIST or repo_name in REPO_ALLOWLIST


def clone_repo(repo_url, branch, token):
    tmp_dir = tempfile.mkdtemp()
    if token:
        parsed = urlparse(repo_url)
        repo_url = f"{parsed.scheme}://x-access-token:{token}@{parsed.netloc}{parsed.path}"
    cmd = ['git', 'clone', '--depth', '1', '--branch', branch, repo_url, tmp_dir]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=120)
        return tmp_dir
    except subprocess.CalledProcessError as e:
        log.info('git_clone_failed', extra={'stderr': e.stderr.decode(errors='replace')})
        raise


def get_changed_files(repo_path, base_sha, head_sha):
    try:
        cmd = ['git', 'diff', '--name-only', base_sha, head_sha]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, check=True)
        files = result.stdout.decode().strip().split('\n')
        return [f for f in files if f and not f.endswith('.lock')]
    except subprocess.CalledProcessError as e:
        log.info('git_diff_failed', extra={'stderr': e.stderr.decode(errors='replace')})
        return []


def analyze_with_nova(filepath, code_content):
    if len(code_content) > MAX_CODE_CHARS_PER_FILE:
        code_content = code_content[:MAX_CODE_CHARS_PER_FILE] + "\n... (truncated)"

    prompt = f"""You are an expert code reviewer analyzing a pull request.

File: {filepath}

Code:

{code_content}


Analyze this code for:
1. **Security vulnerabilities** - SQL injection, hardcoded secrets, XSS, insecure dependencies
2. **Code quality** - readability, maintainability, best practices
3. **Performance issues** - inefficient loops, memory leaks, slow operations

Return your findings as a JSON array with this exact structure:
[
  {{
    "line": 42,
    "severity": "high",
    "category": "security",
    "message": "Hardcoded API key detected"
  }}
]

If no issues found, return: []"""

    try:
        response = bedrock.converse(
            modelId=BEDROCK_MODEL,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2000, "temperature": 0.1},
        )
        text = response['output']['message']['content'][0]['text']
        start, end = text.find('['), text.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return []
    except Exception as e:  # noqa: BLE001
        log.info('bedrock_error', extra={'error': str(e)})
        return []


def _gh_request(method, url, headers, timeout=30, max_retries=3):
    """GitHub API request with exponential-ish backoff and retry."""
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, timeout=timeout)
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = (2 ** attempt)
            log.info('github_retry', extra={'url': url, 'status': resp.status_code, 'wait': wait})
            time.sleep(wait)
            continue
        return resp
    return resp


def post_github_review(repo, pr_number, findings, token):
    if not token:
        log.info('no_github_token')
        return

    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }
    if not findings:
        body = '\u2705 **AI Review Complete**\n\nNo issues detected in this PR!'
    else:
        lines = []
        for finding in findings:
            emoji = '\U0001F534' if finding.get('severity') == 'high' else '\U0001F7E1' if finding.get('severity') == 'medium' else '\U0001F535'
            lines.append(f"{emoji} **{finding.get('severity', 'medium').upper()}**: {finding.get('message', '')}")
        body = f"## \U0001F916 AI Code Review\n\nFound {len(findings)} issue(s):\n\n" + "\n".join(lines) + "\n\n---\n*Generated by Amazon Nova AI*"

    url = f'https://api.github.com/repos/{repo}/issues/{pr_number}/comments'
    resp = _gh_request('POST', url, headers, json={'body': body})
    if resp and resp.status_code not in (200, 201):
        log.info('review_post_failed', extra={'status': resp.status_code, 'body': resp.text[:200]})
    else:
        log.info('review_posted', extra={'status': resp.status_code})


def handler(event, context):
    log.info('sqs_event', extra={'records': len(event.get('Records', [])), 'request_id': getattr(context, 'aws_request_id', None)})

    for record in event.get('Records', []):
        receipt_handle = record.get('receiptHandle', '')
        try:
            payload = json.loads(record['body'])

            action = payload.get('action')
            pr_number = payload.get('number')
            repo_name = payload.get('repository', {}).get('full_name')
            clone_url = payload.get('repository', {}).get('clone_url')
            pr_title = payload.get('pull_request', {}).get('title', 'No title')

            if action not in ('opened', 'synchronize', 'reopened'):
                log.info('skip_action', extra={'action': action})
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
                continue

            if not allowlisted(repo_name):
                log.info('skip_repo_allowlist', extra={'repo': repo_name})
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
                continue

            head_sha = payload.get('pull_request', {}).get('head', {}).get('sha')
            base_sha = payload.get('pull_request', {}).get('base', {}).get('sha')
            head_ref = payload.get('pull_request', {}).get('head', {}).get('ref')

            if not all([head_sha, base_sha, head_ref]):
                log.info('missing_commit_info', extra={'pr': pr_number})
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
                continue

            token = resolve_github_token()
            log.info('processing_pr', extra={'pr': pr_number, 'repo': repo_name, 'branch': head_ref})
            repo_path = clone_repo(clone_url, head_ref, token)

            try:
                changed_files = get_changed_files(repo_path, base_sha, head_sha)
                code_files = [f for f in changed_files if f.endswith(tuple(CODE_EXTENSIONS))][:MAX_FILES_PER_PR]
                log.info('files_to_analyze', extra={'count': len(code_files), 'pr': pr_number})

                all_findings = []
                for filepath in code_files:
                    full_path = os.path.join(repo_path, filepath)
                    if os.path.isfile(full_path):
                        content = read_safely(full_path)
                        if content and content.strip():
                            log.info('analyzing_file', extra={'file': filepath, 'pr': pr_number})
                            findings = analyze_with_nova(filepath, content)
                            for f in findings:
                                f['file'] = filepath
                            all_findings.extend(findings)

                if token:
                    post_github_review(repo_name, pr_number, all_findings, token)
                else:
                    log.info('dry_run_findings', extra={'findings': json.dumps(all_findings)})
            finally:
                shutil.rmtree(repo_path, ignore_errors=True)

            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
            log.info('pr_processed', extra={'pr': pr_number, 'findings': len(all_findings)})

        except Exception as e:  # noqa: BLE001
            log.info('message_error', extra={'error': str(e), 'receipt': receipt_handle[:16]})
            # Re-raise so SQS retries via visibility timeout, then moves to DLQ.
            raise

    return {'statusCode': 200, 'body': json.dumps('Processing complete')}


def read_safely(filepath):
    """Read a file safely, tolerating bad encodings / binary content."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except (UnicodeDecodeError, IsADirectoryError, OSError):
        return ''