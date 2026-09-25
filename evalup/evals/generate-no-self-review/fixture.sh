#!/bin/bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; PORT=18731
. "$HERE/../fixtures/scaffold-common.sh"
PLUGIN="$(cd "$HERE/../.." && pwd)"
mkdir -p app/.evalup
cp "$PLUGIN/examples/quickstart/.evalup/profile.yaml" app/.evalup/profile.yaml
sed 's#\${HELPDESK_BASE_URL}#http://127.0.0.1:18731#' \
  "$PLUGIN/examples/quickstart/.evalup/adapter.yaml" > app/.evalup/adapter.yaml
cp "$HERE/../fixtures/findings.md" app/.evalup/findings.md
