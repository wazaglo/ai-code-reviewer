"""GitLab provider implementation."""
import hashlib
import hmac
import logging
import os
import time
from typing import Any

import requests

from . import CodeHostProvider, PRContext, PRFile

log = logging.getLogger('pr-reviewer.provider.gitlab')

CODE_EXTENSIONS = {'.py', '.js', '.ts', '.java', '.go', '.rb', '.php', '.c', '.cpp', '.h', '.cs', '.rs'}
MAX_FILES_PER_MR = 10


class GitLabProvider(CodeHostProvider):
    @property
    def name(self) -> str:
        return 'gitlab'

    @property
    def webhook_event_types(self) -> list[str]:
        return ['merge_request']

    def verify_signature(self, secret: str, body: bytes, headers: dict[str, str]) -> bool:
        # GitLab supports both token-based and HMAC-based verification
        # For HMAC: X-Gitlab-Token header contains the secret token
        # For token: X-Gitlab-Token contains the webhook secret token
        token = headers.get('X-Gitlab-Token') or headers.get('x-gitlab-token') or ''
        if not token:
            return False
        return hmac.compare_digest(token, secret)

    def extract_pr_context(self, payload: dict[str, Any]) -> PRContext | None:
        # Only process merge_request events with action: open, update, reopen
        object_kind = payload.get('object_kind')
        if object_kind != 'merge_request':
            return None

        action = payload.get('object_attributes', {}).get('action')
        if action not in ('open', 'update', 'reopen'):
            return None

        mr = payload.get('object_attributes', {})
        mr_iid = mr.get('iid')
        project = payload.get('project', {})
        project_path = project.get('path_with_namespace')
        gitlab_url = project.get('web_url', '').replace('/' + project_path, '') if project.get('web_url') else ''

        if not project_path or not mr_iid:
            return None

        token = os.environ.get('GITLAB_TOKEN', '')
        api_base = os.environ.get('GITLAB_API_URL', 'https://gitlab.com/api/v4')

        return PRContext(
            provider='gitlab',
            repo=project_path,
            pr_number=mr_iid,
            token=token,
            api_base=api_base
        )

    def fetch_pr_files(self, context: PRContext) -> list[PRFile]:
        # GitLab uses project ID (URL-encoded path) and MR IID
        project_id = requests.utils.quote(context.repo, safe='')
        url = f'{context.api_base}/projects/{project_id}/merge_requests/{context.pr_number}/changes'
        resp = self._gl_request('GET', url, context.token)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(f'GitLab API changes failed: {resp.status_code if resp else "no response"}')

        data = resp.json()
        changes = data.get('changes', [])

        files = []
        for change in changes:
            filepath = change.get('new_path') or change.get('old_path') or ''
            diff = change.get('diff', '')
            ext = os.path.splitext(filepath)[1]
            if ext in CODE_EXTENSIONS and diff:
                files.append(PRFile(
                    filename=filepath,
                    patch=diff,
                    status=change.get('new_file', False) and 'added' or change.get('deleted_file', False) and 'removed' or 'modified'
                ))
        return files[:MAX_FILES_PER_MR]

    def post_review_comment(self, context: PRContext, body: str) -> bool:
        project_id = requests.utils.quote(context.repo, safe='')
        url = f'{context.api_base}/projects/{project_id}/merge_requests/{context.pr_number}/notes'
        resp = self._gl_request('POST', url, context.token, json={'body': body})
        if resp is None or resp.status_code not in (200, 201):
            log.error('gitlab_comment_failed', extra={'status': resp.status_code if resp else 'none'})
            return False
        return True

    def get_repo_identifier(self, payload: dict[str, Any]) -> str:
        project = payload.get('project', {})
        return project.get('path_with_namespace', '')

    def _gl_request(self, method: str, url: str, token: str, max_retries: int = 4, **kwargs) -> requests.Response | None:
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        for attempt in range(max_retries):
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                retry_after = int(resp.headers.get('Retry-After', 0) or 0)
                wait = retry_after or (2 ** attempt)
                log.info('gitlab_retry', extra={'url': url, 'status': resp.status_code, 'wait': wait})
                time.sleep(min(wait, 30))
                continue
            return resp
        return None