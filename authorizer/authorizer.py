import json
import os
import time
import urllib.request

import jwt

AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
USER_POOL_ID = os.environ['USER_POOL_ID']
JWKS_URL = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"

_cache = {'keys': None, 'fetched': 0}

def _fetch_jwks():
    now = time.time()
    if _cache['keys'] and (now - _cache['fetched']) < 300:
        return _cache['keys']
    with urllib.request.urlopen(JWKS_URL, timeout=10) as resp:
        _cache['keys'] = json.loads(resp.read().decode())
        _cache['fetched'] = now
    return _cache['keys']

def _verify_token(token_str):
    try:
        headers = jwt.get_unverified_header(token_str)
        jwks = _fetch_jwks()
        kid = headers.get('kid')
        public_key = None
        for key in jwks.get('keys', []):
            if key.get('kid') == kid:
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
                break
        if not public_key:
            return None, "Public key not found in JWKS"
        claims = jwt.decode(
            token_str,
            key=public_key,
            algorithms=['RS256'],
            issuer=f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}",
            options={'verify_aud': False},
        )
        client_id = os.environ.get('CLIENT_ID', '')
        token_client = claims.get('client_id') or claims.get('aud')
        if client_id and token_client != client_id:
            return None, "Token client_id mismatch"
        return claims, None
    except jwt.ExpiredSignatureError:
        return None, "Token expired"
    except jwt.InvalidTokenError as e:
        return None, f"Invalid token: {str(e)}"
    except Exception as e:
        return None, f"Verification error: {str(e)}"

def handler(event, context):
    method_arn = event['methodArn']
    headers = event.get('headers', {}) or {}
    # API Gateway may lowercase headers or use different casing
    user_agent = headers.get('User-Agent', '') or headers.get('user-agent', '') or headers.get('User-Agent', '')
    token = headers.get('Authorization', '') or headers.get('authorization', '')

    if user_agent.startswith('GitHub'):
        print(json.dumps({'event': 'AUTH_ALLOWED', 'reason': 'GitHub webhook exemption', 'status': 200}))
        return generate_policy('user', 'Allow', method_arn)

    if not token:
        print(json.dumps({'event': 'AUTH_DENIED', 'reason': 'No token provided', 'status': 401}))
        return generate_policy('user', 'Deny', method_arn)

    if not token.startswith('Bearer '):
        print(json.dumps({'event': 'AUTH_DENIED', 'reason': 'No Bearer prefix', 'status': 401}))
        return generate_policy('user', 'Deny', method_arn)

    token_str = token[7:]
    _, error = _verify_token(token_str)
    if error:
        print(json.dumps({'event': 'AUTH_DENIED', 'reason': error, 'status': 401}))
        return generate_policy('user', 'Deny', method_arn)

    return generate_policy('user', 'Allow', method_arn)

def generate_policy(principal_id, effect, resource):
    return {
        'principalId': principal_id,
        'policyDocument': {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Action': 'execute-api:Invoke',
                    'Effect': effect,
                    'Resource': resource,
                }
            ]
        }
    }
