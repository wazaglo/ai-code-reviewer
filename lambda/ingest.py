"""Webhook ingress - verifies the GitHub signature before enqueueing to SQS.

This function sits between API Gateway (Lambda proxy) and the SQS queue. It is
the only code that sees the raw HTTP request, so it is the correct place to
enforce **who** is allowed to submit review requests.

It:
  1. Reads and caches the webhook secret from AWS Secrets Manager.
  2. Computes the HMAC-SHA256 of the raw body using the GitHub secret.
  3. Rejects the request (401) unless the `X-Hub-Signature-256` matches.
  4. Enqueues the validated payload to the review queue and returns 200.

Even a valid signature from an approved sender is still rate-limited at the
API Gateway stage and WAF layer (see the CloudFormation template).
"""
import hashlib
import hmac
import json
import os

import boto3

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
WEBSECRET_ARN = os.environ.get('WEBSECRET_ARN', '')
QUEUE_URL = os.environ.get('QUEUE_URL', '')

secretsmanager = boto3.client('secretsmanager', region_name=AWS_REGION)
sqs = boto3.client('sqs', region_name=AWS_REGION)

_webhook_secret = None


def _get_webhook_secret():
    """Fetch + cache the GitHub webhook secret from Secrets Manager."""
    global _webhook_secret
    if _webhook_secret is None and WEBSECRET_ARN:
        resp = secretsmanager.get_secret_value(SecretId=WEBSECRET_ARN)
        _webhook_secret = resp['SecretString']
    return _webhook_secret or ''


def _verification_enabled():
    """An empty webhook secret disables signature verification (dry-run)."""
    return bool(_get_webhook_secret())


def _verify_signature(secret, body, signature_header):
    """Constant-time compare of GitHub's HMAC signature."""
    if not signature_header:
        return False
    expected = 'sha256=' + hmac.new(
        secret.encode('utf-8'), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handler(event, context):
    body = event.get('body', '') or ''
    # Lambda proxy delivers the raw body as a (possibly base64-encoded) string.
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body)
    else:
        body = body.encode('utf-8')

    headers = event.get('headers', {})
    # GitHub may send lowercase header names depending on proxy.
    signature = headers.get('X-Hub-Signature-256') or headers.get('x-hub-signature-256') or ''

    secret = _get_webhook_secret()

    if _verification_enabled() and not _verify_signature(secret, body, signature):
        print(json.dumps({'event': 'INVALID_SIGNATURE', 'status': 401}))
        return {
            'statusCode': 401,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid webhook signature'}),
        }

    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=body.decode('utf-8'))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({'event': 'ENQUEUE_FAILED', 'error': str(e), 'status': 500}))
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Failed to enqueue message'}),
        }

    print(json.dumps({'event': 'ENQUEUED', 'status': 200}))
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'message': 'Webhook received and queued'}),
    }