# AI Code Reviewer

A **production-hardened, fully-managed** serverless system that automatically reviews **GitHub Pull Requests** and **GitLab Merge Requests** using Amazon Bedrock (Nova). It flags security vulnerabilities (hardcoded secrets, SQL injection, insecure CORS), code-quality issues, and performance problems, then posts actionable feedback as a comment on the PR/MR — asynchronously, securely, and at low cost.

**Multi-provider:** GitHub and GitLab (including self-hosted) from a single deployment.

**Cost attribution:** Per-provider, per-repo, per-month token tracking in DynamoDB.

No waiting for human reviewers. No scaling worries. Deploy once, connect any repo, and let the AI review every PR/MR.

---

## Architecture

```
                     ┌────────────────┐    ┌──────────┐    ┌─────────────────┐
   GitHub/GitLab     │  Ingest Lambda │───►│   SQS    │───►│   Worker Lambda  │
   webhook (POST)    │ detects provider│    │ + DLQ    │   │ fetch diffs      │
        │            │ verifies HMAC   │    └──────────┘   │ Bedrock Nova    │
        │            └────────────────┘                   │ post comment    │
        │  ┌───────────┐   ┌───────────┐                  └────────┬────────┘
        └─►│ WAF       │──►│ API GW    │                           │
           │ rate rule │   │ throttling│                           ▼
           └───────────┘   └───────────┘                  ┌─────────────────┐
                                                          │   Bedrock Nova   │
                                                          └─────────────────┘
                                                                    │
                                                                    ▼
                                                          ┌─────────────────┐
                                                          │ DynamoDB Cost   │
                                                          │ Attribution      │
                                                          └─────────────────┘
```

### Flow

1. **GitHub/GitLab** sends a webhook `POST` to the public endpoint.
2. **API Gateway** (rate-limited) forwards raw body + headers to **Ingest Lambda**.
3. **Ingest Lambda** detects provider (GitHub/GitLab), verifies signature (HMAC-SHA256 for GitHub, token for GitLab) — rejects unsigned requests (401).
4. Validated payload dropped onto **SQS** (with **dead-letter queue**). Webhook returns `200 OK` instantly — Git provider never waits on AI.
5. **Worker Lambda** consumes message, uses provider abstraction to fetch changed files via provider's REST API, analyzes each file with **Bedrock Nova**.
6. Findings posted back as **inline PR/MR comment**. Token usage recorded to **DynamoDB** for cost attribution per provider/org/repo/month.

---

## Repository Layout

```
├── lambda/
│   ├── ingest.py                    # Webhook verifier (multi-provider HMAC + enqueue)
│   ├── lambda_function.py           # PR/MR review worker (SQS -> Provider API -> Bedrock -> comment)
│   └── provider/                    # Provider abstraction layer
│       ├── __init__.py              # Base classes (CodeHostProvider, PRContext, PRFile)
│       ├── github.py                # GitHub implementation
│       ├── gitlab.py                # GitLab implementation
│       └── factory.py               # Provider detection & instantiation
├── cloudformation/
│   └── template.yaml                # Entire stack: API GW, Lambdas, SQS, DLQ, Secrets, DynamoDB, WAF, Alarms, Budget
├── deploy/
│   └── deploy.sh                    # Packages code -> uploads to S3 -> deploys stack
├── config/
│   └── model_config.yaml            # Bedrock model selection (single source of truth)
├── .github/workflows/deploy.yml     # CI/CD: lint + auto-deploy on push to main
└── README.md
```

---

## Production Hardening — 5 Upgrade Areas + Multi-Provider + Cost Attribution

### 1. Webhook Signature Verification ✅
**Problem:** Any client guessing the URL could inject payloads and burn Bedrock tokens. Throttling limited *volume*, not *who*.
**Fix:** Dedicated **Ingest Lambda** verifies signatures per-provider:
- **GitHub:** HMAC-SHA256 via `X-Hub-Signature-256`
- **GitLab:** Secret token via `X-Gitlab-Token`
Requests not signed by the provider are rejected `401`. Configure the same secret in (a) provider webhook settings, (b) deploy flags (`--webhook-secret` / `--gitlab-webhook-secret`).

### 2. Secrets in AWS Secrets Manager ✅
**Problem:** Tokens lived in Lambda env vars / CLI args — visible in template dumps and shell history.
**Fix:** **GitHub token**, **GitHub webhook secret**, **GitLab token**, **GitLab webhook secret** stored in **AWS Secrets Manager**. Lambdas fetch at runtime (cached per warm invocation). Nothing sensitive in code, templates, or CI logs. Rotate in place, no redeploy.

### 3. Dead-Letter Queue (DLQ) ✅
**Problem:** Failing messages retried forever via SQS visibility timeout — blocking queue, jamming availability.
**Fix:** Source queue has **RedrivePolicy** with `maxReceiveCount` (default **5**). After 5 failures, message moves to **`-dlq`** queue for inspection/replay without stalling traffic. DLQ URL in stack outputs.

### 4. Continuous Deployment (CI/CD) ✅
**Problem:** Deploys were manual, unrepeatable, laptop-dependent.
**Fix:** **GitHub Actions workflow** (`.github/workflows/deploy.yml`) on push to `main`:
- **Lints** Lambda files (`py_compile`) + CloudFormation (`cfn-lint`)
- **Deploys** via `deploy.sh` using **OpenID Connect** (no long-lived AWS keys in CI)
- **Concurrency locking** prevents colliding pushes
Tokens from GitHub **secrets** (`GITHUB_TOKEN_PAT`, `GITHUB_WEBHOOK_SECRET`, `GITLAB_TOKEN`, `GITLAB_WEBHOOK_SECRET`); config from **variables** (`BEDROCK_MODEL_ID`, `AWS_DEPLOY_ROLE_ARN`, `GITLAB_API_URL`, `COST_TABLE_NAME`).

### 5. Observability & Cost Controls ✅
- **CloudWatch Alarms** on `Lambda Errors` (worker + ingest) and API `5XXError`
- **AWS Budget** (default $50/mo) emails on spend threshold
- **X-Ray active tracing** on both Lambdas — API → SQS → Bedrock → Provider call graph
- **Structured JSON logs** for parseable, queryable CloudWatch Logs
- **DynamoDB Cost Attribution** — token usage per `provider#repo#month`

Plus abuse protection (still enabled):
- **API Gateway stage throttling** — hard `requests/sec` + burst caps (defaults 10/20). Excess → `429`.
- **AWS WAF rate-based rule** — blocks single client IP exceeding N requests/5min (default 200).

---

## Multi-Provider Support

| Feature | GitHub | GitLab (SaaS / Self-Hosted) |
|---------|--------|----------------------------|
| Webhook event | `pull_request` | `merge_request` |
| Signature | `X-Hub-Signature-256` (HMAC) | `X-Gitlab-Token` (secret token) |
| API base | `https://api.github.com` | `https://gitlab.com/api/v4` (configurable) |
| Auth | `token <PAT>` | `Bearer <PAT>` |
| Diff endpoint | `/repos/{owner}/{repo}/pulls/{num}/files` | `/projects/{id}/merge_requests/{iid}/changes` |
| Comment endpoint | `/repos/{owner}/{repo}/issues/{num}/comments` | `/projects/{id}/merge_requests/{iid}/notes` |
| Repo identifier | `org/repo` | `group/project` |

**Single webhook URL** handles both providers — Ingest Lambda auto-detects from headers.

---

## Cost Attribution (DynamoDB)

Every review records token usage to `PR-Review-CostAttribution` (configurable name):

| Key | Example |
|-----|---------|
| **PK** | `github#acme/backend` or `gitlab#infra/terraform` |
| **SK** | `2026-01` (month) |
| **Attributes** | `prompt_tokens`, `completion_tokens`, `total_tokens`, `estimated_cost_usd`, `review_count`, `last_updated`, `ttl` (90 days) |

Query costs:
```bash
# All entries
aws dynamodb scan --table-name PR-Review-CostAttribution

# Specific repo/month
aws dynamodb get-item --table-name PR-Review-CostAttribution \
  --key '{"pk":{"S":"gitlab#acme/backend"},"sk":{"S":"2026-01"}}'
```

---

## Model Configuration (One Place)

Edit `config/model_config.yaml` and pass `--model` to deploy. Single `BedrockModelId` drives **both** runtime invocation **and** IAM permission ARN.

| Model | Trade-off |
|-------|-----------|
| `amazon.nova-pro-v1:0` | Most capable / higher cost |
| `amazon.nova-lite-v1:0` | Balanced speed & quality (recommended) |
| `amazon.nova-micro-v1:0` | Fastest / cheapest |

> **Before deploying:** Enable the chosen model in [Bedrock console](https://console.aws.amazon.com/bedrock) → *Model access*.

---

## Deployment

### Prerequisites

- AWS CLI authenticated with permissions: IAM, S3, Lambda, API Gateway, SQS, Secrets Manager, Bedrock, WAF, Budgets, **DynamoDB**
- Python 3.9+, `pip`, `zip`
- **GitHub** fine-grained PAT (scopes: `Contents` read, `Pull requests` read/write) — for GitHub repos
- **GitLab** PAT (scope: `api`) — for GitLab repos/projects
- Chosen **Bedrock model enabled** in *Model access*

### One-Shot Deploy

```bash
chmod +x deploy/deploy.sh

./deploy/deploy.sh \
  --region us-east-1 \
  --stack-name pr-reviewer \
  --github-token 'ghp_...' \
  --webhook-secret 'my-github-webhook-secret' \
  --gitlab-token 'glpat-...' \
  --gitlab-webhook-secret 'my-gitlab-webhook-secret' \
  --model amazon.nova-lite-v1:0 \
  --allowlist 'github:myorg/myrepo,gitlab:mygroup/myproject' \
  --notify-email 'engineering@example.com'
```

### All Deploy Options

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | `us-east-1` | AWS region |
| `--stack-name` | `pr-reviewer` | CloudFormation stack name |
| `--github-token` | (none) | GitHub PAT (private repos) |
| `--webhook-secret` | (none) | GitHub webhook secret for HMAC |
| `--gitlab-token` | (none) | GitLab PAT (api scope) |
| `--gitlab-webhook-secret` | (none) | GitLab webhook secret token |
| `--gitlab-api-url` | `https://gitlab.com/api/v4` | GitLab API base (self-hosted) |
| `--model` | from `config/model_config.yaml` | Bedrock model ID |
| `--code-bucket` | auto-created | S3 bucket for artifacts |
| `--rate-limit` | `10` | API requests/second |
| `--burst-limit` | `20` | API concurrent burst |
| `--waf-limit` | `200` | WAF requests/IP/5 min |
| `--max-receive-count` | `5` | SQS retries before DLQ |
| `--allowlist` | (all) | Comma-separated `provider:org/repo` |
| `--budget` | `50` | Monthly USD spend budget |
| `--notify-email` | (none) | Email for budget/cost alerts |
| `--cost-table-name` | `PR-Review-CostAttribution` | DynamoDB table name |
| `--no-package` | off | Skip packaging/upload |
| `--dry-run` | off | Print command and exit |

> **Allowlist format:** `github:org/repo,gitlab:group/project`. Empty = allow all.

---

## Connecting Repositories

### GitHub
1. Repo → **Settings → Webhooks → Add webhook**
2. **Payload URL:** `WebhookUrl` stack output
3. **Content type:** `application/json`
4. **Secret:** same as `--webhook-secret`
5. **Events:** *Let me select individual events* → **Pull requests**
6. **Active:** on → **Save**

### GitLab (SaaS or Self-Hosted)
1. Project → **Settings → Webhooks → Add webhook**
2. **URL:** same `WebhookUrl` stack output
3. **Secret token:** same as `--gitlab-webhook-secret`
4. **Trigger:** **Merge request events** (open, update, reopen)
5. **Enable SSL verification:** on (or off for self-hosted without valid cert)
6. **Add webhook**

Now every PR/MR (opened / synchronized / reopened) is automatically reviewed. Output:

```
## 🤖 AI Code Review

Found 2 issue(s):

🔴 **HIGH**: Hardcoded API key detected in config.py
🟡 **MEDIUM**: Unbounded loop on user-provided input in processor.py

---
*Generated by Amazon Nova AI*
```

---

## CI/CD (Recommended for Real Repos)

The included GitHub Actions workflow deploys automatically. To use in **this** repository:

1. Create a **deploy role** in AWS assumable via OIDC, set:
   - `vars.AWS_DEPLOY_ROLE_ARN` — the role ARN
2. Add **repository secrets**:
   - `GITHUB_TOKEN_PAT` (GitHub PAT)
   - `GITHUB_WEBHOOK_SECRET`
   - `GITLAB_TOKEN` (GitLab PAT, optional)
   - `GITLAB_WEBHOOK_SECRET` (optional)
3. Add **repository variables**:
   - `BEDROCK_MODEL_ID`
   - `GITLAB_API_URL` (if self-hosted)
   - `COST_TABLE_NAME` (if custom)

Push to `main` → linted → deployed. No manual step.

---

## Observability & Ops

```bash
# Tail worker logs (structured JSON)
aws logs tail /aws/lambda/PR-Review-Worker --follow

# Find webhook rejections
aws logs filter-log-events \
  --log-group-name "API-Gateway-Execution-Logs_<api-id>/prod" \
  --filter-pattern 'INVALID_SIGNATURE|failed'

# Inspect dead-letter queue
aws sqs receive-message --queue-url "<DlqUrl stack output>"

# Query cost attribution
aws dynamodb scan --table-name PR-Review-CostAttribution
```

- **Alarms:** `PR-Review-Worker-errors`, `PR-Review-Ingest-errors`, `PR-Reviewer-API-5xx`
- **X-Ray:** AWS X-Ray → trace map for end-to-end webhook flow
- **Budgets:** Email when spend crosses configured monthly limit
- **DynamoDB:** Per-provider/repo/month token usage + estimated cost

---

## Costs (Approximate, Per-Use)

- **API Gateway:** ~$3.50 / 1M requests
- **SQS:** ~$0.40 / 1M requests (after free tier)
- **Lambda:** CU-based; tiny per invocation (512 MB, up to 300 s)
- **DynamoDB:** On-demand; negligible for cost attribution writes
- **Bedrock (Nova):** Per-token — dominant cost. Contained by throttling, WAF, repo allowlist, per-PR cap of **10 files × 6000 chars**

---

## Troubleshooting

| Symptom | Likely Cause / Fix |
|---------|--------------------|
| Webhook returns `401 Invalid webhook signature` | `--webhook-secret` / `--gitlab-webhook-secret` doesn't match provider webhook settings |
| Webhook returns `429` | Over rate/WAF limit — raise `--rate-limit` / `--waf-limit` |
| `AccessDeniedException` from Bedrock | Model not enabled in *Model access*, or `--model` ≠ enabled model |
| "Git clone failed" / "MR fetch failed" | Private repo missing valid `--github-token` / `--gitlab-token` |
| Messages stuck, not processed | Check **DLQ**; poison message may be redriving |
| "Missing commit info, skipping" | Payload isn't PR/MR event — check webhook **Content type = application/json** |
| GitLab self-hosted: cert errors | Set `--gitlab-api-url` to your instance; disable SSL verification in webhook if needed |

---

## License

MIT