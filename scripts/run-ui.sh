#!/usr/bin/env bash
#
# Launch the Community Scout demo UI (Streamlit).
#
# The UI invokes the DEPLOYED Amazon Bedrock AgentCore Runtime using the standard AWS
# credential chain. Configure via environment variables if needed:
#
#   AWS_REGION              (default: us-east-1)
#   AGENTCORE_RUNTIME_ARN   (default: the deployed Community Scout Runtime ARN)
#
# Usage:
#   ./scripts/run-ui.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

exec streamlit run ui/app.py
