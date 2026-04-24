#!/usr/bin/env bash
# Build the pjsua-mcp image and push it to an internal Harbor registry.
#
# Usage:
#   ./scripts/publish.sh <version>            # e.g. v0.3.0 — tags :<version> + :latest
#   ./scripts/publish.sh <version> --no-latest  # skip the :latest tag (for pre-releases)
#
# Configuration is read from ./.env (docker-compose already reads it for UID/GID):
#   HARBOR_HOST=harbor.example.corp
#   HARBOR_PROJECT=voip-tools
#   HARBOR_IMAGE=pjsua-mcp                    # optional, defaults to "pjsua-mcp"
#
# Prerequisites:
#   - `docker login "$HARBOR_HOST"` already done once (credentials cached in ~/.docker/config.json).
#   - For multi-arch publish: pass --platform linux/amd64,linux/arm64 (uses `docker buildx`).

set -euo pipefail

# -------- args --------
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <version> [--no-latest] [--platform <list>]" >&2
  exit 1
fi

VERSION="$1"
shift
PUSH_LATEST=1
PLATFORM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-latest) PUSH_LATEST=0; shift ;;
    --platform)  PLATFORM="$2"; shift 2 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "WARNING: version '$VERSION' does not look like vMAJOR.MINOR.PATCH — pushing anyway." >&2
fi

# -------- config --------
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  # Read only HARBOR_* keys — `source .env` would fail on read-only bash
  # built-ins like UID that docker-compose consumes from the same file.
  while IFS='=' read -r key value; do
    case "$key" in
      HARBOR_HOST|HARBOR_PROJECT|HARBOR_IMAGE)
        # Strip surrounding quotes if any.
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        export "$key=$value"
        ;;
    esac
  done < .env
fi

: "${HARBOR_HOST:?Set HARBOR_HOST in .env (e.g. harbor.example.corp)}"
: "${HARBOR_PROJECT:?Set HARBOR_PROJECT in .env (e.g. voip-tools)}"
HARBOR_IMAGE="${HARBOR_IMAGE:-pjsua-mcp}"

REF="$HARBOR_HOST/$HARBOR_PROJECT/$HARBOR_IMAGE"

echo ">>> Publishing $REF:$VERSION"
if [[ "$PUSH_LATEST" -eq 1 ]]; then
  echo "    (also tagging :latest)"
fi
if [[ -n "$PLATFORM" ]]; then
  echo "    platforms: $PLATFORM (via buildx)"
fi

# -------- verify login --------
if ! docker info --format '{{json .RegistryConfig}}' >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable" >&2
  exit 1
fi

if ! grep -q "\"$HARBOR_HOST\"" ~/.docker/config.json 2>/dev/null; then
  echo "WARNING: no cached credentials for $HARBOR_HOST in ~/.docker/config.json." >&2
  echo "         Run \`docker login $HARBOR_HOST\` if push fails." >&2
fi

# -------- build + push --------
if [[ -n "$PLATFORM" ]]; then
  # buildx: build and push in one step (no local image left behind).
  ARGS=(--platform "$PLATFORM" --tag "$REF:$VERSION")
  if [[ "$PUSH_LATEST" -eq 1 ]]; then
    ARGS+=(--tag "$REF:latest")
  fi
  docker buildx build "${ARGS[@]}" --push .
else
  # Classic path: build once, tag, push both.
  docker build -t "$REF:$VERSION" .
  if [[ "$PUSH_LATEST" -eq 1 ]]; then
    docker tag "$REF:$VERSION" "$REF:latest"
  fi
  docker push "$REF:$VERSION"
  if [[ "$PUSH_LATEST" -eq 1 ]]; then
    docker push "$REF:latest"
  fi
fi

echo ">>> Done. Pull with:"
echo "    docker pull $REF:$VERSION"
