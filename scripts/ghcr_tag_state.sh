#!/usr/bin/env bash
# Print `present` or `absent` for one GHCR image tag (ghcr.io/<owner>/<name>:<tag>).
# Any other registry answer (401, 403, 5xx, a network error) exits 1, so the caller
# never builds and pushes again over a tag that exists (platform-images.yml, #1822).
# Credentials: GHCR_USER and GHCR_TOKEN (a token with packages:read or more).
set -euo pipefail

image="${1:?usage: ghcr_tag_state.sh ghcr.io/<owner>/<name>:<tag>}"
ref="${image#ghcr.io/}"
repo="${ref%%:*}"
tag="${ref#*:}"
if [ "$ref" = "$image" ] || [ "$repo" = "$ref" ] || [ -z "$tag" ] || [ -z "$repo" ]; then
  echo "not a ghcr.io/<owner>/<name>:<tag> image: $image" >&2
  exit 2
fi

if ! body="$(curl -fsS --max-time 30 -u "${GHCR_USER:?}:${GHCR_TOKEN:?}" \
  "https://ghcr.io/token?service=ghcr.io&scope=repository:${repo}:pull")"; then
  echo "GHCR refused a pull token for ${repo}" >&2
  exit 1
fi
token="$(printf '%s' "$body" | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')"

accept="application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"
accept="${accept}, application/vnd.docker.distribution.manifest.list.v2+json"
accept="${accept}, application/vnd.docker.distribution.manifest.v2+json"
code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' -I \
  -H "Authorization: Bearer ${token}" -H "Accept: ${accept}" \
  "https://ghcr.io/v2/${repo}/manifests/${tag}")" || code="no answer"

case "$code" in
  200) echo present ;;
  404) echo absent ;;
  *)
    echo "GHCR answered '${code}' for ${image}; not 200 (present) or 404 (absent)" >&2
    exit 1
    ;;
esac
