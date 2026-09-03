"""GitHub provider implementation."""
import hashlib
import hmac
import logging
import os
import time
from typing import Any

import requests

from . import CodeHostProvider, PRContext, PRFile

log = logging.getLogger('pr-reviewer.provider.github')

GITHUB_API = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
CODE_EXTENSIONS = {'.py', '.js', '.ts', '.java', '.go', '.rb', '.php', '.c', '.cpp', '.h', '.cs', '.rs'}
MAX_FILES_PER_PR = 10


class GitHubProvider(CodeHostProvider):
    @property
    def name(self) -> str:
        return 'github'

    @property
    def webhook_event_types(self) -> list[str]:
        return ['pull_request']

    def verify_signature(self, secret: str, body: bytes, headers: dict[str, str]) -> bool:
        signature = headers.get('X-Hub-Signature-256') or headers.get('x-hub-signature-256') or ''
        if not signature:
            return False
        expected = 'sha256=' + hmac.new(
            secret.encode('utf-8'), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def extract_pr_context(self, payload: dict[str, Any]) -> PRContext | None:
        action = payload.get('action')
        if action not in ('opened', 'synchronize', 'reopened'):
            return None

        pr_number = payload.get('number')
        repo_name = payload.get('repository', {}).get('full_name')
        if not repo_name or not pr_number:
            return None

        token = os.environ.get('GITHUB_TOKEN', '')
        return PRContext(
            provider='github',
            repo=repo_name,
            pr_number=pr_number,
            token=token,
            api_base=GITHUB_API
        )

    def fetch_pr_files(self, context: PRContext) -> list[PRFile]:
        url = f'{context.api_base}/repos/{context.repo}/pulls/{context.pr_number}/files?per_page=100'
        resp = self._gh_request('GET', url, context.token)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(f'GitHub API files failed: {resp.status_code if resp else "no response"}')

        files = []
        for f in resp.json():
            ext = os.path.splitext(f.get('filename', ''))[1]
            if ext in CODE_EXTENSIONS and f.get('patch'):
                files.append(PRFile(
                    filename=f['filename'],
                    patch=f['patch'],
                    status=f.get('status', '')
                ))
        return files[:MAX_FILES_PER_PR]

    def post_review_comment(self, context: PRContext, body: str) -> bool:
        url = f'{context.api_base}/repos/{context.repo}/issues/{context.pr_number}/comments'
        resp = self._gh_request('POST', url, context.token, json={'body': body})
        if resp is None or resp.status_code not in (200, 201):
            log.error('github_comment_failed', extra={'status': resp.status_code if resp else 'none'})
            return False
        return True

    def get_repo_identifier(self, payload: dict[str, Any]) -> str:
        return payload.get('repository', {}).get('full_name', '')

    def merge_pr(self, context: PRContext) -> bool:
        """Merge the pull request. Return True on success."""
        url = f'{context.api_base}/repos/{context.repo}/pulls/{context.pr_number}/merge'
        resp = self._gh_request('PUT', url, context.token)
        if resp is None or resp.status_code not in (200, 201):
            log.error('github_merge_failed', extra={'status': resp.status_code if resp else 'none'})
            return False
        log.info('github_merged', extra={'pr': context.pr_number, 'repo': context.repo})
        return True

    def close_pr(self, context: PRContext) -> bool:
        """Close the pull request. Return True on success."""
        url = f'{context.api_base}/repos/{context.repo}/pulls/{context.pr_number}'
        resp = self._gh_request('PATCH', url, context.token, json={'state': 'closed'})
        if resp is None or resp.status_code not in (200, 201):
            log.error('github_close_failed', extra={'status': resp.status_code if resp else 'none'})
            return False
        log.info('github_closed', extra={'pr': context.pr_number, 'repo': context.repo})
        return True

    def _gh_request(self, method: str, url: str, token: str, max_retries: int = 4, **kwargs) -> requests.Response | None:
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        for attempt in range(max_retries):
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                retry_after = int(resp.headers.get('Retry-After', 0) or 0)
                wait = retry_after or (2 ** attempt)
                log.info('github_retry', extra={'url': url, 'status': resp.status_code, 'wait': wait})
                time.sleep(min(wait, 30))
                continue
            return resp
        return None
