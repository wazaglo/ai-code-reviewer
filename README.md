# AI Code Reviewer

A fully-managed, serverless system that automatically reviews GitHub Pull Requests using Amazon Bedrock (Nova). It flags security vulnerabilities (hardcoded secrets, SQL injection, insecure CORS), code-quality issues, and performance problems, then posts actionable feedback as a comment on the PR — all without waiting on human reviewers.

```
                    ┌─────────────┐     ┌─────────┐     ┌────────────────┐     ┌─────────┐
  GitHub webhook ──►│ API Gateway │ ──► │   SQS   │ ──► │    Lambda      │ ──► │ Bedrock │
  (POST /webhook)   │  WAF+wafile │     │  Queue  │     │ PR-Review-Work │     │  Nova   │
                    │  throttling │     │ (async) │     │ er             │ ──► │         │
                    └─────────────┘     └─────────┘          │          └─────────┘
                                                              └── post comment ──► GitHub
```

## Why this architecture

- **Asynchronous & non-blocking** — The webhook returns immediately (`200 OK`) after dropping the event into SQS, so GitHub never sees a timeout even when a review takes minutes.
- **Decoupled & resilient** — SQS buffers bursts and re-drives failures; a transient AI error doesn't lose the event.
- **One-click reproducible** — The whole stack is defined in a CloudFormation template, so teammates or other environments are identical.
- **Cost & abuse protected** — Layered throttling (below) caps usage so a leaked endpoint can't drain your AI tokens.

## Repository layout

```
├── lambda/
│   └── lambda_function.py        # The review worker (SQS -> clone -> Bedrock -> GitHub)
├── cloudformation/
│   └── template.yaml             # Full stack definition (one-shot)
├── deploy/
│   └── deploy.sh                 # Packages code, uploads to S3, deploys stack
├── config/
│   └── model_config.yaml         # Where you change the Bedrock model
└── README.md
```

---

## Model configuration (one place)

Edit `config/model_config.yaml` and pass `--model` to the deploy script. The **single `BedrockModelId` parameter** drives both the runtime invocation **and** the IAM permission ARN, so there is only one thing to change.

| Model | Trade-off |
|-------|-----------|
| `amazon.nova-pro-v1:0` | Most capable / highest cost |
| `amazon.nova-lite-v1:0` | Balanced speed & quality (recommended) |
| `amazon.nova-micro-v1:0` | Fastest / cheapest |

> **Before deploying:** enable the model in the [Bedrock console](https://console.aws.amazon.com/bedrock) → *Model access*, e.g. `amazon.nova-lite-v1:0` in `us-east-1`.

## Protection against token abuse

The public webhook is guarded by **two independent limits** so that even if the URL leaks, attackers can't run up your Bedrock bill:

1. **API Gateway stage throttling** — a hard cap on requests/second (`--rate-limit`, default **10 rps**) and concurrent burst (`--burst-limit`, default **20**). Excess traffic is rejected with `429 Too Many Requests` before it ever reaches SQS.

2. **AWS WAF rate-based rule** — limits requests from a **single client IP** to `--waf-limit` (default **200**) per rolling 5 minutes, then blocks that source. This stops a leaked URL from being hammered by one bot farm.

Both limits are configurable at deploy time and are enabled on the production stage by default.

---

## Deployment

### Prerequisites

- AWS CLI (`aws`) authenticated with credentials that can create IAM roles, S3 buckets, Lambda, API Gateway, SQS, Bedrock, and WAF.
- Python 3.9+ with `pip` (used to build the `requests` layer).
- `zip` available on the build host.
- Model enabled in Bedrock *Model access*.

### One-shot deploy

```bash
chmod +x deploy/deploy.sh

./deploy/deploy.sh \
  --region us-east-1 \
  --stack-name pr-reviewer \
  --github-token 'ghp_your_token_here' \
  --model amazon.nova-lite-v1:0
```

The script:

```text
==> Region:      us-east-1
==> Stack:       pr-reviewer
==> Model:       amazon.nova-lite-v1:0
==> Code bucket: s3://pr-reviewer-artifacts-<account>
==> Packaging Lambda code...
==> Building 'requests' layer...
==> Ensuring S3 bucket exists...
==> Uploading artifacts...
==> Deploying CloudFormation stack 'pr-reviewer'...
==> Stack outputs: ...
==> Done. Register the WebhookUrl output as your GitHub webhook.
```

When the stack finishes, note the **`WebhookUrl`** output:

```
https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/webhook
```

### All deploy options

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | `us-east-1` | AWS region |
| `--stack-name` | `pr-reviewer` | CloudFormation stack name |
| `--github-token` | (none) | GitHub PAT (scope `repo`) — leave empty for public repos |
| `--model` | `amazon.nova-2-lite-v1:0` | Bedrock model ID |
| `--code-bucket` | auto-created | S3 bucket for packaged artifacts |
| `--rate-limit` | `10` | API requests/second |
| `--burst-limit` | `20` | API concurrent burst |
| `--waf-limit` | `200` | WAF requests/IP/5 min |
| `--no-package` | off | Skip packaging/upload (artifacts already in S3) |
| `--dry-run` | off | Print the deploy command and exit |

> For private repositories, `--github-token` is **required** or the Lambda cannot clone the repo.

---

## Connecting a GitHub repository

1. In GitHub, open your repo → **Settings → Webhooks → Add webhook**.
2. **Payload URL:** the `WebhookUrl` output.
3. **Content type:** `application/json`.
4. **Events:** select *Let me select individual events* and check **Pull requests**.
5. **Active:** on. Save.

A new/updated PR now triggers the flow automatically. The Lambda posts either a **no-issues** confirmation or a comment listing findings:

```
## 🤖 AI Code Review

Found 2 issue(s):

🔴 **HIGH**: Hardcoded API key detected
🟡 **MEDIUM**: Unbounded loop on user-provided input

---
*Generated by Amazon Nova AI*
```

---

## What the reviewer checks

The prompt (in `lambda/lambda_function.py`) asks Nova to analyze each changed code file for:

1. **Security** — SQL injection, hardcoded secrets, XSS, dependency risks.
2. **Code quality** — readability, maintainability, best practices.
3. **Performance** — inefficient loops, memory leaks, slow operations.

The response is parsed as a JSON array and normalized to `severity × category × message` findings, which become the PR comment. Files are limited to the 10 most recently changed code files per PR to bound cost.

### Supported code file types

`.py`, `.js`, `.ts`, `.java`, `.go`, `.rb`, `.php`, `.c`, `.cpp`, `.h`, `.cs`, `.rs`

---

## How the pieces fit

| Component | What it does |
|-----------|--------------|
| **API Gateway** | Exposes `POST /webhook`; validates and forwards the payload to SQS; returns `200` immediately. |
| **SQS Queue** | Buffers events; decouples ingestion from processing; provides retries via visibility timeout. |
| **Lambda worker** | Consumes one message at a time, shallow-clones the PR branch, computes changed files, calls Bedrock for each, and posts the summary back to GitHub. |
| **Bedrock (Nova)** | Performs the actual code analysis. |
| **WAF + throttling** | Rate-limits inbound traffic to protect token spend. |

### IAM roles created

- **Lambda execution role** (`WorkerRole`) — scoped `sqs:ReceiveMessage/DeleteMessage`, `bedrock:InvokeModel` on the selected model only, and CloudWatch Logs.
- **API Gateway role** (`ApiGatewayRole`) — `sqs:SendMessage` to the review queue only.

---

## Observability

- **API Gateway** logs every request/response (method, integration, status) to `API-Gateway-Execution-Logs_<api-id>/prod`.
- **Lambda** logs its steps (`Received SQS event…`, `Processing PR #N…`, `Analyzing <file>…`) and any errors to `/aws/lambda/<function-name>`.
- **WAF** samples traffic per rule.

```bash
# Tail Lambda logs
aws logs tail /aws/lambda/PR-Review-Worker --follow

# Inspect a webhook round-trip
aws logs filter-log-events \
  --log-group-name "API-Gateway-Execution-Logs_<api-id>/prod" \
  --filter-pattern "Error|Failed"
```

---

## Costs (approximate, us-east-1)

Charges are **pay-per-use**; there is no idle cost once the stack is built.

- **API Gateway** — ~$3.50 per million requests.
- **SQS** — ~$0.40 per million requests after the 1M free tier.
- **Lambda** — CU-based; tiny per invocation (512 MB, up to 300 s).
- **Bedrock (Nova)** — per-token; the dominant variable. Fixed by throttling + file/code limits.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `AccessDeniedException` from Bedrock | Model not enabled in **Model access**, or `BedrockModelId` doesn't match the enabled model. |
| Webhook returns `429` | Hitting the stage rate/waf limit — raise `--rate-limit` / `--waf-limit`. |
| "Git clone failed" | Private repo without a valid `--github-token`. |
| "Missing commit info, skipping" | Webhook payload is not a GitHub PR event (test messages / wrong content-type). Use `Content type: application/json`. |
| `Failed to download layer` | Layer package invalid — run with packaging enabled. |

## License

MIT