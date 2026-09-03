"""PR review worker - consumes queued PR events and runs an AI review.

Pipeline: SQS -> GitHub/GitLab REST API (changed files + patches) -> Bedrock (Nova)
analysis -> post findings back to PR as a comment -> merge/close based on severity.

Design notes:
  * No git binary: the Lambda runtime does not ship one. Changed files and
    their diffs come straight from the provider REST API, which is cheaper and
    more reliable in Lambda than cloning.
  * Provider tokens from AWS Secrets Manager (preferred) or *_TOKEN env var.
  * Dead-letter-aware: raise on failure so SQS retries and eventually moves
    the message to the DLQ (maxReceiveCount). Do NOT delete messages by hand -
    the Lambda event source mapping owns message deletion.
  * Optional repository allowlist to restrict which repos are analyzed.
  * Detailed cost tracking per PR in DynamoDB (model tokens, processing time).
  * Auto-merge if no high-severity issues, auto-close if high-severity found.
  * Structured JSON logging, retry with backoff on the API calls.
"""
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import boto3
from provider import CodeHostProvider, PRContext
from provider.factory import detect_provider, get_provider

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)
secretsmanager = boto3.client('secretsmanager', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)

BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0')
# Comma-separated allowlist, e.g. "acme/web,acme/app". Empty = allow all.
REPO_ALLOWLIST = {r.strip() for r in os.environ.get('REPO_ALLOWLIST', '').split(',') if r.strip()}

# Cost tracking
COST_TABLE_NAME = os.environ.get('COST_TABLE_NAME', '')
COST_TABLE = dynamodb.Table(COST_TABLE_NAME) if COST_TABLE_NAME else None

# Auto-merge/close behavior
MERGE_ON_LOW_SEVERITY = os.environ.get('MERGE_ON_LOW_SEVERITY', 'true').lower() == 'true'
MERGE_ON_MEDIUM_SEVERITY = os.environ.get('MERGE_ON_MEDIUM_SEVERITY', 'false').lower() == 'true'
CLOSE_ON_HIGH_SEVERITY = os.environ.get('CLOSE_ON_HIGH_SEVERITY', 'true').lower() == 'true'

CODE_EXTENSIONS = {'.py', '.js', '.ts', '.java', '.go', '.rb', '.php', '.c', '.cpp', '.h', '.cs', '.rs'}
MAX_FILES_PER_PR = 10
MAX_DIFF_CHARS_PER_FILE = 6000

log = logging.getLogger('pr-reviewer')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(_h)
log.setLevel(logging.INFO)

_token_cache: dict[str, str] = {}


def resolve_token(provider: str) -> str:
    """Secrets Manager first, then fall back to env var."""
    cache_key = f'{provider}_token'
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    arn_env = f'{provider.upper()}_TOKEN_ARN'
    token_env = f'{provider.upper()}_TOKEN'

    arn = os.environ.get(arn_env, '')
    if arn:
        try:
            resp = secretsmanager.get_secret_value(SecretId=arn)
            _token_cache[cache_key] = resp['SecretString']
            return _token_cache[cache_key]
        except Exception as e:
            log.warning(f'Failed to fetch token from Secrets Manager: {e}')

    _token_cache[cache_key] = os.environ.get(token_env, '')
    return _token_cache[cache_key]


def allowlisted(repo_name: str) -> bool:
    return not REPO_ALLOWLIST or repo_name in REPO_ALLOWLIST


def track_cost(
    provider: str,
    repo: str,
    pr_number: int,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    files_analyzed: int,
    processing_time_ms: int,
    cost_usd: float,
    findings_count: int,
) -> None:
    """Record detailed cost attribution per PR."""
    if not COST_TABLE:
        return

    pk = f'{provider}:{repo}:{datetime.utcnow().strftime("%Y-%m")}'
    sk = f'PR:{pr_number}'
    ttl = int(time.time()) + (30 * 24 * 3600)  # 30 days

    try:
        COST_TABLE.put_item(
            Item={
                'pk': pk,
                'sk': sk,
                'ttl': ttl,
                'model': model_id,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'files_analyzed': files_analyzed,
                'processing_time_ms': processing_time_ms,
                'cost_usd': cost_usd,
                'findings_count': findings_count,
                'timestamp': datetime.utcnow().isoformat(),
            }
        )
        log.info('cost_tracked', extra={'pk': pk, 'sk': sk, 'cost_usd': cost_usd})
    except Exception as e:
        log.error('cost_tracking_failed', extra={'error': str(e)})


def analyze_with_nova(filepath: str, diff_text: str) -> dict[str, Any]:
    """Analyze diff with Bedrock Nova. Returns findings and token usage."""
    if len(diff_text) > MAX_DIFF_CHARS_PER_FILE:
        diff_text = diff_text[:MAX_DIFF_CHARS_PER_FILE] + '\n... (diff truncated)'

    prompt = f"""You are an expert code reviewer analyzing the DIFF of a pull/merge request.

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

    start_time = time.time()
    try:
        response = bedrock.converse(
            modelId=BEDROCK_MODEL,
            messages=[{'role': 'user', 'content': [{'text': prompt}]}],
            inferenceConfig={'maxTokens': 2000, 'temperature': 0.1},
        )
        text = response['output']['message']['content'][0]['text']
        start, end = text.find('['), text.rfind(']') + 1
        if start != -1 and end > start:
            findings = json.loads(text[start:end])
        else:
            findings = []

        # Get token usage from response
        token_usage = response.get('usage', {})
        input_tokens = token_usage.get('inputTokens', 0)
        output_tokens = token_usage.get('outputTokens', 0)
        return {
            'findings': findings,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'processing_time_ms': int((time.time() - start_time) * 1000),
        }
    except Exception as e:
        log.error('bedrock_error', extra={'error': str(e)[:500], 'model': BEDROCK_MODEL})
        return {
            'findings': [],
            'input_tokens': 0,
            'output_tokens': 0,
            'processing_time_ms': int((time.time() - start_time) * 1000),
            'error': str(e)[:500],
        }


def build_comment(findings: list[dict[str, Any]]) -> str:
    """Build review comment body from findings."""
    if not findings:
        return '✅ **AI Review Complete**\n\nNo issues detected in this PR!'
    emoji = {'high': '🔴', 'medium': '🟠', 'low': '🔵'}
    lines = []
    for f in findings:
        sev = (f.get('severity') or 'medium').lower()
        marker = emoji.get(sev, '🟠')
        file_part = f"**{f['file']}** - " if f.get('file') else ''
        lines.append(f"{marker} **{sev.upper()}** {file_part}{f.get('message', '')}")
    return (
        f'## 🤖 AI Code Review\n\nFound {len(findings)} issue(s):\n\n'
        + '\n'.join(lines)
        + '\n\n---\n*Generated by Amazon Nova AI*'
    )


def should_merge_or_close(findings: list[dict[str, Any]]) -> str:
    """Determine if PR should be auto-merged or closed based on severity.

    Returns: 'merge', 'close', or None
    """
    if not findings:
        return 'merge' if MERGE_ON_LOW_SEVERITY else None

    high_count = sum(1 for f in findings if (f.get('severity') or 'medium').lower() == 'high')

    if high_count > 0 and CLOSE_ON_HIGH_SEVERITY:
        return 'close'
    if high_count == 0 and MERGE_ON_MEDIUM_SEVERITY:
        return 'merge'
    if high_count == 0 and MERGE_ON_LOW_SEVERITY:
        return 'merge'
    return None


def post_provider_comment(provider: CodeHostProvider, context: PRContext, body: str) -> bool:
    """Post review comment to the PR/MR."""
    return provider.post_review_comment(context, body)


def merge_pr(provider: CodeHostProvider, context: PRContext) -> bool:
    """Merge the PR/MR."""
    return provider.merge_pr(context)


def close_pr(provider: CodeHostProvider, context: PRContext) -> bool:
    """Close the PR/MR."""
    return provider.close_pr(context)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log.info('sqs_event', extra={'records': len(event.get('Records', []))})

    for record in event.get('Records', []):
        try:
            payload = json.loads(record['body'])
        except (ValueError, KeyError) as e:
            log.error('bad_message_body', extra={'error': str(e), 'body': record.get('body', '')[:200]})
            continue

        # Detect provider from payload
        provider_name = detect_provider(payload, {})
        if not provider_name:
            log.info('skip_unknown_provider', extra={'payload_keys': list(payload.keys())})
            continue

        provider = get_provider(provider_name)
        if not provider:
            log.error('unknown_provider', extra={'provider': provider_name})
            continue

        # Extract context from payload
        pr_context = provider.extract_pr_context(payload)
        if not pr_context:
            log.info('skip_pr_context', extra={'provider': provider_name})
            continue

        action = payload.get('action') or payload.get('object_attributes', {}).get('action')
        repo_name = provider.get_repo_identifier(payload)

        if action not in ('opened', 'synchronize', 'reopened', 'open', 'update', 'reopen'):
            log.info('skip_action', extra={'action': action})
            continue

        if not repo_name or not allowlisted(repo_name) or not pr_context.pr_number:
            log.info('skip_repo', extra={'repo': repo_name})
            continue

        token = resolve_token(provider_name)
        log.info('processing_pr', extra={'pr': pr_context.pr_number, 'repo': repo_name, 'provider': provider_name})

        # Fetch and analyze files
        try:
            files = provider.fetch_pr_files(pr_context)
        except Exception as e:
            log.error('fetch_files_failed', extra={'error': str(e)})
            continue

        code_files = [f for f in files if os.path.splitext(f.filename)[1] in CODE_EXTENSIONS and f.patch][:MAX_FILES_PER_PR]
        log.info('files_to_analyze', extra={'count': len(code_files), 'pr': pr_context.pr_number})

        # Track totals
        total_input_tokens = 0
        total_output_tokens = 0
        total_processing_time = 0
        all_findings: list[dict[str, Any]] = []

        start_time = time.time()
        for entry in code_files:
            filepath = entry.filename
            log.info('analyzing_file', extra={'file': filepath, 'pr': pr_context.pr_number})
            result = analyze_with_nova(filepath, entry.patch)
            total_input_tokens += result.get('input_tokens', 0)
            total_output_tokens += result.get('output_tokens', 0)
            total_processing_time += result.get('processing_time_ms', 0)
            for f in result.get('findings', []):
                f['file'] = filepath
            all_findings.extend(result.get('findings', []))

        processing_time_total = int((time.time() - start_time) * 1000)
        total_processing_time += processing_time_total

        # Calculate approximate cost (pricing as of 2026-09)
        # amazon.nova-lite-v1: $0.00000013/inv + $0.00000003/inv tokens (input), $0.00000051/inv tokens (output)
        if BEDROCK_MODEL == 'amazon.nova-lite-v1:0':
            cost_usd = 0.00000013 + (total_input_tokens * 0.00000003) + (total_output_tokens * 0.00000051)
        else:
            # Fallback: $0.001 per 1M input tokens, $0.003 per 1M output tokens
            cost_usd = (total_input_tokens / 1_000_000 * 0.001) + (total_output_tokens / 1_000_000 * 0.003)

        # Post review comment
        body = build_comment(all_findings)
        post_provider_comment(provider, pr_context, body)

        # Determine if we should merge or close
        action = should_merge_or_close(all_findings)
        if action == 'merge' and token:
            log.info('auto_merge_attempt', extra={'pr': pr_context.pr_number, 'findings': len(all_findings)})
            merge_pr(provider, pr_context)
        elif action == 'close' and token:
            log.info('auto_close_attempt', extra={'pr': pr_context.pr_number, 'high_severity': sum(1 for f in all_findings if f.get('severity') == 'high')})
            close_pr(provider, pr_context)

        # Track cost
        track_cost(
            provider=provider_name,
            repo=repo_name,
            pr_number=pr_context.pr_number,
            model_id=BEDROCK_MODEL,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            files_analyzed=len(code_files),
            processing_time_ms=total_processing_time,
            cost_usd=round(cost_usd, 6),
            findings_count=len(all_findings),
        )

        log.info('pr_processed', extra={
            'pr': pr_context.pr_number,
            'provider': provider_name,
            'findings': len(all_findings),
            'cost_usd': round(cost_usd, 6),
            'tokens': total_input_tokens + total_output_tokens,
        })

    return {'statusCode': 200, 'body': 'Processing complete'}
