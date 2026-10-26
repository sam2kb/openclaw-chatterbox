#!/usr/bin/env bash
# Build a local release bundle. Never includes weights, runtime data, or secrets.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$("$ROOT/.venv/bin/python" -c 'import clawvoice; print(clawvoice.__version__)')"
MANIFEST_VERSION="$("$ROOT/.venv/bin/python" -c 'import json; print(json.load(open("deploy/release-manifest.json"))["release"])')"
[ "$VERSION" = "$MANIFEST_VERSION" ] || { echo "version/manifest mismatch: $VERSION vs $MANIFEST_VERSION" >&2; exit 1; }
DIST="$ROOT/dist"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
BUNDLE="openclaw-chatterbox-$VERSION"
OUT="$DIST/$BUNDLE"
rm -rf "$OUT" "$OUT.tar.gz" "$DIST/clawvoice-$VERSION"*.whl
mkdir -p "$OUT"

"$ROOT/.venv/bin/python" -m build --wheel --outdir "$DIST"
WHEEL="$(ls -1t "$DIST"/clawvoice-"$VERSION"*.whl | head -1)"
WHEEL_SHA="$(sha256sum "$WHEEL" | cut -d' ' -f1)"
mkdir -p "$OUT/wheel" "$OUT/skill" "$OUT/deploy" "$OUT/config" "$OUT/docs"
cp "$WHEEL" "$OUT/wheel/"
tar -C skills -cf - chatterbox-voice | tar -C "$OUT/skill" --no-same-permissions -xf -
cp deploy/Dockerfile deploy/compose.yaml deploy/compose.gpu.yaml deploy/compose.cpu.yaml \
   deploy/clawbox deploy/clawbox.py deploy/release.sh deploy/update_manifest.py \
   deploy/release-manifest.json "$OUT/deploy/"
"$ROOT/.venv/bin/python" "$OUT/deploy/update_manifest.py" "$OUT/deploy/release-manifest.json" \
  "$(basename "$WHEEL")" "$WHEEL_SHA"
cp "$OUT/deploy/release-manifest.json" deploy/release-manifest.json
tar -C docs -cf - . | tar -C "$OUT/docs" --no-same-permissions -xf -
cp config/clawvoice.env.example "$OUT/config/"
tar -C . -cf - src web | tar -C "$OUT" --no-same-permissions -xf -
rm -rf "$OUT/src"/*.egg-info "$OUT/src"/*/__pycache__ "$OUT/src"/*.pyc
cp .dockerignore README.md AGENTS.md LICENSE THIRD_PARTY_NOTICES.md pyproject.toml uv.lock "$OUT/"
"$ROOT/.venv/bin/clawvoice" openapi --output "$OUT/docs/api/openapi.json" >/dev/null

chmod -R g-s "$OUT" 2>/dev/null || true
find "$OUT" -type f -name '*.pyc' -delete
find "$OUT" -type d -name '__pycache__' -prune -exec rm -rf {} +
# Fail closed if private/runtime material somehow enters the bundle.
if find "$OUT" -type f \( -name '*.safetensors' -o -name '*.pt' -o -name '*.db' \) \
   -o -type f \( -name '*.wav' -o -name '*.mp3' -o -name '*.webm' -o -name '*.ulaw' -o -name '*.m4a' \) \
   -o -type f \( -name 'service-token' -o -name '.env' -o -name '*.key' -o -name '*.pem' \) | grep -q .; then
  echo "refusing to package weights/data/secrets" >&2
  exit 1
fi
(
  cd "$OUT"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$DIST" -czf "$OUT.tar.gz" "$BUNDLE"
echo "release bundle: $OUT.tar.gz"
sha256sum "$OUT.tar.gz"
