"""PR review worker - consumes queued PR/MR events and runs an AI review.

Pipeline: SQS -> Provider API (changed files + patches) -> Bedrock (Nova)
analysis -> post findings back to provider as a PR/MR comment.

Supports GitHub and GitLab via provider abstraction.
"""
import json
import logging
import os
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from provider.factory import get_provider
from provider import PRContext

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)

BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0')
COST_TABLE_NAME = os.environ.get('COST_TABLE_NAME', '')

MAX_DIFF_CHARS_PER_FILE = 6000

log = logging.getLogger('pr-reviewer')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(_h)
log.setLevel(logging.INFO)

_cost_table = None


def _get_cost_table():
    global _cost_table
    if _cost_table is None and COST_TABLE_NAME:
        _cost_table = dynamodb.Table(COST_TABLE_NAME)
    return _cost_table


def record_cost_attribution(provider: str, repo: str, prompt_tokens: int, completion_tokens: int, model_id: str):
    """Record token usage for cost attribution per provider/org/repo/month."""
    table = _get_cost_table()
    if not table:
        return

    now = datetime.utcnow()
    month_key = now.strftime('%Y-%m')
    pk = f'{provider}#{repo}'

    # Estimate cost (rough approximation for Nova models)
    # Nova pricing varies; using ~$0.0008/1K input, $0.0032/1K output tokens as baseline
    input_cost = (prompt_tokens / 1000) * 0.0008
    output_cost = (completion_tokens / 1000) * 0.0032
    estimated_cost = input_cost + output_cost

    try:
        table.update_item(
            Key={'pk': pk, 'sk': month_key},
            UpdateExpression='ADD prompt_tokens :pt, completion_tokens :ct, total_tokens :tt, estimated_cost_usd :ec, review_count :rc SET last_updated :lu',
            ExpressionAttributeValues={
                ':pt': prompt_tokens,
                ':ct': completion_tokens,
                ':tt': prompt_tokens + completion_tokens,
                ':ec': round(estimated_cost, 6),
                ':rc': 1,
                ':lu': now.isoformat()
            }
        )
    except ClientError as e:
        log.warning('cost_attribution_failed', extra={'error': str(e), 'provider': provider, 'repo': repo})


def analyze_with_nova(filepath: str, diff_text: str):
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

        # Extract token usage for cost attribution
        usage = response.get('usage', {})
        prompt_tokens = usage.get('inputTokens', 0)
        completion_tokens = usage.get('outputTokens', 0)

        return findings, prompt_tokens, completion_tokens
    except Exception as e:
        log.error('bedrock_error', extra={'error': str(e)[:500], 'model': BEDROCK_MODEL})
        raise


def build_comment(findings):
    if not findings:
        return '\u2705 **AI Review Complete**\n\nNo issues detected in this PR/MR!'
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


def handler(event, context):
    log.info('sqs_event', extra={'records': len(event.get('Records', []))})

    for record in event.get('Records', []):
        try:
            payload = json.loads(record['body'])
        except (ValueError, KeyError) as e:
            log.error('bad_message_body', extra={'error': str(e), 'body': record.get('body', '')[:200]})
            continue

        # Detect provider from payload
        provider_name = None
        if 'repository' in payload and 'pull_request' in str(payload):
            provider_name = 'github'
        elif payload.get('object_kind') == 'merge_request':
            provider_name = 'gitlab'

        if not provider_name:
            log.warning('unknown_provider_in_payload', extra={'payload_keys': list(payload.keys())})
            continue

        provider = get_provider(provider_name)
        if not provider:
            log.error('provider_not_found', extra={'provider': provider_name})
            continue

        pr_context = provider.extract_pr_context(payload)
        if not pr_context:
            log.info('skip_action', extra={'provider': provider_name})
            continue

        # Check allowlist (supports both github org/repo and gitlab group/project format)
        allowlist = {r.strip() for r in os.environ.get('REPO_ALLOWLIST', '').split(',') if r.strip()}
        repo_identifier = provider.get_repo_identifier(payload)
        if allowlist and repo_identifier not in allowlist:
            log.info('skip_repo_not_allowlisted', extra={'repo': repo_identifier, 'provider': provider_name})
            continue

        log.info('processing_pr', extra={'provider': provider_name, 'pr': pr_context.pr_number, 'repo': pr_context.repo})

        try:
            changed_files = provider.fetch_pr_files(pr_context)
        except Exception as e:
            log.error('fetch_files_failed', extra={'error': str(e), 'provider': provider_name, 'pr': pr_context.pr_number})
            raise

        log.info('files_to_analyze', extra={'count': len(changed_files), 'provider': provider_name, 'pr': pr_context.pr_number})

        all_findings = []
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for entry in changed_files:
            filepath = entry.filename
            log.info('analyzing_file', extra={'file': filepath, 'provider': provider_name, 'pr': pr_context.pr_number})
            findings, prompt_tokens, completion_tokens = analyze_with_nova(filepath, entry.patch)
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            for f in findings:
                f['file'] = filepath
            all_findings.extend(findings)

        if pr_context.token:
            comment_body = build_comment(all_findings)
            success = provider.post_review_comment(pr_context, comment_body)
            if not success:
                raise RuntimeError(f'Failed to post comment to {provider_name}')
        else:
            log.info('dry_run_findings', extra={'findings': json.dumps(all_findings)[:2000]})

        # Record cost attribution
        record_cost_attribution(provider_name, pr_context.repo, total_prompt_tokens, total_completion_tokens, BEDROCK_MODEL)

        log.info('pr_processed', extra={'provider': provider_name, 'pr': pr_context.pr_number, 'findings': len(all_findings)})

    return {'statusCode': 200, 'body': 'Processing complete'}