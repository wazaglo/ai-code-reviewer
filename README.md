# AI Code Reviewer

Automatically reviews **GitHub Pull Requests** and **GitLab Merge Requests** with **Amazon Bedrock (Nova)**. Every PR/MR you open gets an AI comment flagging security holes (hardcoded secrets, SQL injection), code-quality issues, and performance problems — within seconds, for pennies.

![Architecture](docs/architecture.png)

## How it works

Your Git provider sends a webhook → **API Gateway** (Cognito-protected, rate-limited) → **Ingest Lambda** verifies it's really from GitHub/GitLab → **SQS** queues it → **Worker Lambda** fetches the diff, asks **Bedrock Nova** for a review → posts the comment on your PR.

Live since Phase-A testing — sample of a real review:

![AI review comment](docs/screenshots/ai-review-comment.png)

## Endpoint

```
POST https://qwtyz9sq5i.execute-api.us-east-1.amazonaws.com/prod/webhook
```

Two ways to talk to it:

### 1. Connect a repo (no auth needed — recommended)

Webhooks from GitHub/GitLab are recognized automatically and don't need a token.

**GitHub:** repo → Settings → Webhooks → Add webhook

| Field | Value |
|-------|-------|
| Payload URL | the endpoint above |
| Content type | `application/json` |
| Secret | the value in AWS Secrets Manager (`WebhookSecret-...`) — must match |
| Events | **Pull requests** |

**GitLab:** project → Settings → Webhooks → URL above, trigger **Merge request events**, token = the GitLab secret in Secrets Manager.

Open a PR and watch the AI comment appear. That's it.

### 2. Call the API yourself (Cognito token required)

Everyone else must present a Cognito access token — anonymous callers get `403`.

```bash
# Get a token (ask the admin to create your user in pool us-east-1_b8jDeIgeu)
TOKEN=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id us-east-1_b8jDeIgeu \
  --client-id 1b3ild527g3p199finoma0rvdq \
  --auth-flow ADMIN_NO_SRP_AUTH \
  --auth-parameters USERNAME=you@example.com,PASSWORD='yourpass' \
  --region us-east-1 --query AuthenticationResult.AccessToken --output text)

# Use it
curl -X POST https://qwtyz9sq5i.execute-api.us-east-1.amazonaws.com/prod/webhook \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d @pull-request-payload.json
```

Tokens expire after one hour; re-run the first command to get a new one.

## What protects the endpoint

| Layer | Effect |
|-------|--------|
| Cognito authorizer (Lambda) | Bearer token required — except recognized Git webhooks |
| HMAC signature check | Rejects forged GitHub/GitLab payloads (401) |
| Stage throttling | 10 req/s, burst 20 — excess gets `429` |
| WAF rate rule | Per-IP flood blocking |
| Budget alarm | Email when monthly spend crosses the limit |

## For developers / deploying your own

```bash
git clone https://github.com/wazaglo/ai-code-reviewer
cd ai-code-reviewer
./deploy/deploy.sh --region us-east-1 --github-token 'ghp_...' --webhook-secret 'shared-secret'
```

- Model choice: single line in `config/model_config.yaml`
- Full IaC: `cloudformation/template.yaml`
- CI/CD: push to `main` → lint → deploy (`.github/workflows/deploy.yml`)
- Architecture diagram source: `docs/architecture.py` (`pip install diagrams`)

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Webhook 401 | Hook **Secret** doesn't match the Secrets Manager value |
| API call 403 | Missing/expired `Authorization: Bearer <token>` |
| 429 | Over rate limit — slow down |
| No AI comment | Check Lambda logs: `aws logs tail /aws/lambda/PR-Review-Worker --follow` |

## License

MIT
