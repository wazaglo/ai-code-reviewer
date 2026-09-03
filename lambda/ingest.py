"""Webhook ingress - verifies signatures from GitHub/GitLab before enqueueing to SQS.

This function sits between API Gateway (Lambda proxy) and the SQS queue. It is
the only code that sees the raw HTTP request, so it is the correct place to
enforce **who** is allowed to submit review requests.

It:
  1. Detects the provider (GitHub/GitLab) from headers/payload.
  2. Reads and caches the webhook secret from AWS Secrets Manager.
  3. Verifies the signature using the provider-specific method.
  4. Enqueues the validated payload to the review queue and returns 200.
"""
import json
import os

import boto3

from provider.factory import detect_provider, verify_signature_for_provider

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
WEBSECRET_ARN = os.environ.get('WEBSECRET_ARN', '')
GITLAB_WEBSECRET_ARN = os.environ.get('GITLAB_WEBSECRET_ARN', '')
QUEUE_URL = os.environ.get('QUEUE_URL', '')

secretsmanager = boto3.client('secretsmanager', region_name=AWS_REGION)
sqs = boto3.client('sqs', region_name=AWS_REGION)

_webhook_secrets: dict[str, str] = {}


def _get_webhook_secret(provider: str) -> str:
    """Fetch + cache the webhook secret from Secrets Manager for a provider."""
    if provider in _webhook_secrets:
        return _webhook_secrets[provider]

    arn = WEBSECRET_ARN if provider == 'github' else GITLAB_WEBSECRET_ARN
    if not arn:
        return ''

    try:
        resp = secretsmanager.get_secret_value(SecretId=arn)
        _webhook_secrets[provider] = resp['SecretString']
    except Exception:
        _webhook_secrets[provider] = ''

    return _webhook_secrets[provider]


def _verification_enabled(provider: str) -> bool:
    """An empty webhook secret disables signature verification (dry-run)."""
    return bool(_get_webhook_secret(provider))


def handler(event, context):
    body = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body)
    else:
        body = body.encode('utf-8')

    headers = event.get('headers', {})
    payload = {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        pass

    provider = detect_provider(payload, headers)
    if not provider:
        print(json.dumps({'event': 'UNKNOWN_PROVIDER', 'status': 400}))
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Unable to detect code hosting provider'}),
        }

    secret = _get_webhook_secret(provider)

    if _verification_enabled(provider) and not verify_signature_for_provider(provider, secret, body, headers):
        print(json.dumps({'event': 'INVALID_SIGNATURE', 'provider': provider, 'status': 401}))
        return {
            'statusCode': 401,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid webhook signature'}),
        }

    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=body.decode('utf-8'))
    except Exception as e:
        print(json.dumps({'event': 'ENQUEUE_FAILED', 'error': str(e), 'status': 500}))
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Failed to enqueue message'}),
        }

    print(json.dumps({'event': 'ENQUEUED', 'provider': provider, 'status': 200}))
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'message': 'Webhook received and queued'}),
    }