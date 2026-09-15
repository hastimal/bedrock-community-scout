#!/usr/bin/env bash
#
# Package the Community Scout application for Amazon Bedrock AgentCore Runtime
# direct code deployment.
#
# Produces: dist/community-scout-runtime.zip
#
# The ZIP contains, at its root:
#   - runtime_entrypoint.py   (root-level AgentCore entry-point adapter)
#   - src/                    (the unchanged application source tree)
#   - requirements.txt        (declared runtime dependencies)
#   - <installed dependencies> (Linux/arm64 wheels — see note below)
#
# NOTE ON DEPENDENCIES / ARCHITECTURE:
# AgentCore Runtime executes on Linux (arm64). Several transitive dependencies
# (pydantic-core, cryptography, etc.) ship native extensions, so we must NOT copy the
# local (e.g. macOS) virtualenv. Instead we install dependencies as Linux/arm64 wheels
# into the build directory using pip's cross-platform download flags. Pure-Python and
# arm64-wheel packages install cleanly this way.
#
# We deliberately DO NOT package: .venv, .git, tests, __pycache__, .pytest_cache,
# .kiro, previous dist builds, local AWS credentials, or unrelated dev files.

set -euo pipefail

# --- Resolve paths ---------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

DIST_DIR="${PROJECT_ROOT}/dist"
ZIP_NAME="community-scout-runtime.zip"
ZIP_PATH="${DIST_DIR}/${ZIP_NAME}"

# Target runtime environment for dependency wheels (must match runtime.yaml).
PY_VERSION="3.12"
PLATFORM="manylinux2014_aarch64"   # AgentCore Runtime is Linux/arm64.

# Prefer python3.12 if available; fall back to python3.
PYTHON_BIN="${PYTHON_BIN:-python3}"

# --- Temporary build directory (cleaned on exit) ---------------------------- #
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/community-scout-build.XXXXXX")"
cleanup() {
  rm -rf "${BUILD_DIR}"
}
trap cleanup EXIT

echo ">> Project root : ${PROJECT_ROOT}"
echo ">> Build dir    : ${BUILD_DIR}"
echo ">> Output ZIP   : ${ZIP_PATH}"
echo ">> Target       : Python ${PY_VERSION} / ${PLATFORM}"

# --- 1) Copy application code (entry point + source tree + requirements) ---- #
echo ">> Copying application code..."
cp "${PROJECT_ROOT}/runtime_entrypoint.py" "${BUILD_DIR}/"
cp "${PROJECT_ROOT}/requirements.txt" "${BUILD_DIR}/"

# Copy the src/ package, excluding caches.
mkdir -p "${BUILD_DIR}/src"
( cd "${PROJECT_ROOT}" && \
  find src -type f \
    ! -path '*/__pycache__/*' \
    ! -name '*.pyc' \
    ! -name '*.pyo' \
    -print0 | while IFS= read -r -d '' f; do
      mkdir -p "${BUILD_DIR}/$(dirname "$f")"
      cp "$f" "${BUILD_DIR}/$f"
    done )

# --- 2) Install runtime dependencies as Linux/arm64 wheels ------------------ #
# Only the application's runtime dependencies are installed (not test-only ones).
# hypothesis, pytest and pytest-asyncio are test dependencies and are intentionally
# excluded from the deployment artifact.
echo ">> Installing runtime dependencies (Linux/arm64 wheels)..."
RUNTIME_REQS="${BUILD_DIR}/.runtime-requirements.txt"
grep -Ev '^(hypothesis|pytest|pytest-asyncio)([=<>! ]|$)' \
  "${PROJECT_ROOT}/requirements.txt" > "${RUNTIME_REQS}"

"${PYTHON_BIN}" -m pip install \
  --requirement "${RUNTIME_REQS}" \
  --target "${BUILD_DIR}" \
  --platform "${PLATFORM}" \
  --python-version "${PY_VERSION}" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade

# Remove the temporary runtime-requirements helper from the artifact.
rm -f "${RUNTIME_REQS}"

# --- 3) Scrub anything that must never be packaged -------------------------- #
echo ">> Scrubbing excluded artifacts from the build directory..."
find "${BUILD_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD_DIR}" -type f -name '*.pyc' -delete 2>/dev/null || true
find "${BUILD_DIR}" -type f -name '*.pyo' -delete 2>/dev/null || true
# Defensive: never ship credentials or local env files even if present.
find "${BUILD_DIR}" -type f \( -name '.env' -o -name '.pypirc' -o -name 'credentials' \) -delete 2>/dev/null || true
rm -rf "${BUILD_DIR}/.aws" 2>/dev/null || true

# --- 4) Build the ZIP ------------------------------------------------------- #
echo ">> Creating ZIP archive..."
mkdir -p "${DIST_DIR}"
rm -f "${ZIP_PATH}"   # remove any previous build
( cd "${BUILD_DIR}" && zip -r -q -X "${ZIP_PATH}" . )

echo ">> Done."
echo ">> Artifact: ${ZIP_PATH}"
if command -v du >/dev/null 2>&1; then
  echo ">> Size    : $(du -h "${ZIP_PATH}" | cut -f1)"
fi
