#!/usr/bin/env bash
#
# deploy.sh - one-shot deployment of the AI Code Reviewer stack.
#
# Packages the Lambda code + `requests` layer, uploads them to S3, and deploys
# the CloudFormation stack. Model selection and throttling are configurable.
#
# Usage:
#   ./deploy/deploy.sh \
#       --region us-east-1 \
#       --stack-name pr-reviewer \
#       --github-token 'ghp_...' \
#       --model amazon.nova-lite-v1:0
#
# Optional flags:
#   --code-bucket NAME    Reuse an existing S3 bucket (default: <stack>-artifacts-<account>)
#   --rate-limit N        API requests/second (default 10)
#   --burst-limit N       API concurrent burst  (default 20)
#   --waf-limit N         WAF requests/IP/5min  (default 200)
#   --no-package          Skip packaging/upload; assume artifacts already in place
#   --dry-run             Print the deploy command and exit without running
#
set -euo pipefail

# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------
REGION="us-east-1"
STACK_NAME="pr-reviewer"
GITHUB_TOKEN=""
MODEL="amazon.nova-2-lite-v1:0"
CODE_BUCKET=""
RATE_LIMIT="10"
BURST_LIMIT="20"
WAF_LIMIT="200"
DO_PACKAGE=1
DRY_RUN=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAMBDA_FILE="${ROOT_DIR}/lambda/lambda_function.py"
TEMPLATE_FILE="${ROOT_DIR}/cloudformation/template.yaml"

# ----------------------------------------------------------------------
# Parse arguments
# ----------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)       REGION="$2"; shift 2 ;;
    --stack-name)   STACK_NAME="$2"; shift 2 ;;
    --github-token) GITHUB_TOKEN="$2"; shift 2 ;;
    --model)        MODEL="$2"; shift 2 ;;
    --code-bucket)  CODE_BUCKET="$2"; shift 2 ;;
    --rate-limit)   RATE_LIMIT="$2"; shift 2 ;;
    --burst-limit)  BURST_LIMIT="$2"; shift 2 ;;
    --waf-limit)    WAF_LIMIT="$2"; shift 2 ;;
    --no-package)   DO_PACKAGE=0; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)              echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
CODE_BUCKET="${CODE_BUCKET:-${STACK_NAME}-artifacts-${ACCOUNT_ID}}"
KEY_PREFIX="pr-reviewer"

echo "==> Region:      ${REGION}"
echo "==> Stack:       ${STACK_NAME}"
echo "==> Model:       ${MODEL}"
echo "==> Code bucket: s3://${CODE_BUCKET}"

# ----------------------------------------------------------------------
# 1. Package Lambda code + requests layer -> S3
# ----------------------------------------------------------------------
if [[ "${DO_PACKAGE}" == "1" ]]; then
  TMPDIR="$(mktemp -d)"
  trap 'rm -rf "${TMPDIR}"' EXIT

  echo "==> Packaging Lambda code..."
  cp "${LAMBDA_FILE}" "${TMPDIR}/lambda_function.py"
  ( cd "${TMPDIR}" && zip -q -r lambda_function.zip lambda_function.py )

  echo "==> Building 'requests' layer..."
  mkdir -p "${TMPDIR}/layer/python"
  pip install requests --target "${TMPDIR}/layer/python" --quiet
  find "${TMPDIR}/layer/python/bin" -type f -delete 2>/dev/null || true
  rmdir "${TMPDIR}/layer/python/bin" 2>/dev/null || true
  ( cd "${TMPDIR}/layer" && zip -q -r "${TMPDIR}/requests-layer.zip" python )

  echo "==> Ensuring S3 bucket exists..."
  if ! aws s3api head-bucket --bucket "${CODE_BUCKET}" --region "$REGION" 2>/dev/null; then
    aws s3api create-bucket --bucket "${CODE_BUCKET}" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi

  echo "==> Uploading artifacts..."
  aws s3 cp "${TMPDIR}/lambda_function.zip"  "s3://${CODE_BUCKET}/${KEY_PREFIX}/lambda_function.zip"  --region "$REGION" --quiet
  aws s3 cp "${TMPDIR}/requests-layer.zip"   "s3://${CODE_BUCKET}/${KEY_PREFIX}/requests-layer.zip"   --region "$REGION" --quiet
fi

# ----------------------------------------------------------------------
# 2. Deploy CloudFormation stack
# ----------------------------------------------------------------------
DEPLOY_ARGS=(
  --region "$REGION"
  --stack-name "$STACK_NAME"
  --template-file "$TEMPLATE_FILE"
  --capabilities CAPABILITY_IAM
  --parameter-overrides \
      CodeBucket="$CODE_BUCKET" \
      CodeKeyPrefix="$KEY_PREFIX" \
      BedrockModelId="$MODEL" \
      ApiStageRateLimit="$RATE_LIMIT" \
      ApiStageBurstLimit="$BURST_LIMIT" \
      WafRateLimit="$WAF_LIMIT" \
      GitHubToken="$GITHUB_TOKEN"
)

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "==> [dry-run] Would run:"
  echo "    aws cloudformation deploy ${DEPLOY_ARGS[*]}"
  exit 0
fi

echo "==> Deploying CloudFormation stack '${STACK_NAME}'..."
aws cloudformation deploy "${DEPLOY_ARGS[@]}" --no-fail-on-empty-changeset

echo "==> Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table

echo "==> Done. Register the WebhookUrl output as your GitHub webhook."