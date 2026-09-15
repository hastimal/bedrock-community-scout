#!/usr/bin/env bash
#
# Deploy the Community Scout AgentCore Runtime (direct code deployment) via
# CloudFormation.
#
# Steps:
#   1. Package the Runtime ZIP (scripts/package-runtime.sh).
#   2. Ensure an S3 artifact bucket exists for this account/region.
#   3. Upload dist/community-scout-runtime.zip to S3.
#   4. Deploy infra/cloudformation/runtime.yaml with `aws cloudformation deploy`,
#      passing the existing Gateway URL and the S3 artifact location.
#   5. Print useful CloudFormation outputs.
#
# This script does NOT invoke the Runtime after deployment.
#
# Configurable via environment variables:
#   AWS_REGION        (default: us-east-1)
#   STACK_NAME        (default: community-scout-runtime)
#   ARTIFACT_BUCKET   (default: community-scout-runtime-artifacts-<account-id>)
#   GATEWAY_URL       (default: the existing Community Scout gateway URL)
#   BEDROCK_MODEL_ID  (default: us.anthropic.claude-sonnet-4-6)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-community-scout-runtime}"
TEMPLATE="${PROJECT_ROOT}/infra/cloudformation/runtime.yaml"
ZIP_PATH="${PROJECT_ROOT}/dist/community-scout-runtime.zip"
# CODE_S3_KEY is intentionally NOT fixed here — a unique, immutable key is generated
# per deployment AFTER packaging (see step 1) so CloudFormation always sees a changed
# CodeConfiguration and creates a new AgentCore Runtime version.

GATEWAY_URL="${GATEWAY_URL:-https://community-scout-gateway-ulm0mt55tz.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"

# --- Resolve the AWS account id -------------------------------------------- #
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-community-scout-runtime-artifacts-${ACCOUNT_ID}}"

echo ">> Region          : ${AWS_REGION}"
echo ">> Account         : ${ACCOUNT_ID}"
echo ">> Stack           : ${STACK_NAME}"
echo ">> Artifact bucket : ${ARTIFACT_BUCKET}"
echo ">> Gateway URL     : ${GATEWAY_URL}"
echo ">> Model           : ${BEDROCK_MODEL_ID}"

# --- 1) Package ------------------------------------------------------------- #
echo ">> [1/5] Packaging Runtime artifact..."
bash "${SCRIPT_DIR}/package-runtime.sh"
if [[ ! -f "${ZIP_PATH}" ]]; then
  echo "ERROR: expected artifact not found at ${ZIP_PATH}" >&2
  exit 1
fi

# Generate a UNIQUE, IMMUTABLE S3 key per deployment so CloudFormation detects a
# CodeConfiguration change and creates a new AgentCore Runtime version. We combine the
# current git commit SHA with a UTC timestamp — the timestamp guarantees uniqueness even
# when there are uncommitted changes (same SHA) or the repo is not a git checkout.
GIT_SHA="$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || echo nogit)"
BUILD_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CODE_S3_KEY="community-scout-runtime/${GIT_SHA}-${BUILD_TIMESTAMP}.zip"
echo ">> CodeS3Key       : ${CODE_S3_KEY}"

# --- 2) Ensure the artifact bucket exists ----------------------------------- #
echo ">> [2/5] Ensuring S3 artifact bucket exists..."
if aws s3api head-bucket --bucket "${ARTIFACT_BUCKET}" --region "${AWS_REGION}" 2>/dev/null; then
  echo "   Bucket already exists: ${ARTIFACT_BUCKET}"
else
  echo "   Creating bucket: ${ARTIFACT_BUCKET}"
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" --region "${AWS_REGION}"
  else
    aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}"
  fi
  # Enable versioning so a re-uploaded artifact of the same key is tracked.
  aws s3api put-bucket-versioning --bucket "${ARTIFACT_BUCKET}" \
    --versioning-configuration Status=Enabled --region "${AWS_REGION}"
  # Block public access on the artifact bucket.
  aws s3api put-public-access-block --bucket "${ARTIFACT_BUCKET}" --region "${AWS_REGION}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi

# --- 3) Upload the artifact ------------------------------------------------- #
echo ">> [3/5] Uploading artifact to s3://${ARTIFACT_BUCKET}/${CODE_S3_KEY}..."
aws s3 cp "${ZIP_PATH}" "s3://${ARTIFACT_BUCKET}/${CODE_S3_KEY}" --region "${AWS_REGION}"

# --- 4) Deploy the CloudFormation stack ------------------------------------- #
echo ">> [4/5] Deploying CloudFormation stack..."
aws cloudformation deploy \
  --template-file "${TEMPLATE}" \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "CodeS3Bucket=${ARTIFACT_BUCKET}" \
    "CodeS3Key=${CODE_S3_KEY}" \
    "GatewayUrl=${GATEWAY_URL}" \
    "BedrockModelId=${BEDROCK_MODEL_ID}"

# --- 5) Print outputs ------------------------------------------------------- #
echo ">> [5/5] Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs" \
  --output table

echo ">> Deployment complete. The Runtime was NOT invoked (by design)."
