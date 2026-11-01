#!/usr/bin/env bash
#
# Regenerate the icon set.
#
#   ./tools/icon/build-icons.sh
#
# Output lands in src/static/icons/, which FastAPI serves under /static/.
# Everything runs in a container: the droplet has neither ImageMagick nor
# librsvg, and this keeps it that way.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT=$(cd ../.. && pwd)
OUT="$ROOT/src/static/icons"

echo "==> Building the toolchain image"
docker build -q -t dailylitbits-iconkit .

mkdir -p "$OUT"
echo "==> Composing the SVGs"
python3 -I mark.py      > "$OUT/favicon.svg"
python3 -I mark.py 0.78 > "$OUT/maskable.svg"

echo "==> Rendering"
docker run --rm -u "$(id -u):$(id -g)" -v "$OUT:/out" dailylitbits-iconkit sh -c '
  set -e
  cd /out
  rsvg-convert -w 180 -h 180 favicon.svg -o apple-touch-icon.png
  rsvg-convert -w 192 -h 192 favicon.svg -o icon-192.png
  rsvg-convert -w 512 -h 512 favicon.svg -o icon-512.png
  rsvg-convert -w 512 -h 512 maskable.svg -o icon-maskable-512.png
  for s in 16 32 48; do rsvg-convert -w $s -h $s favicon.svg -o ico-$s.png; done
  convert ico-16.png ico-32.png ico-48.png favicon.ico
  rm -f ico-*.png maskable.svg
'
echo "Done. Wrote the icon set to src/static/icons/"
