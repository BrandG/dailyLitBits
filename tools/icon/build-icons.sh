#!/usr/bin/env bash
#
# Build the icon set from the artwork in src/.
#
#   ./tools/icon/build-icons.sh
#
# Nothing is drawn here. src/ holds the artwork:
#
#   tile-master.webp       the calendar-and-book mark on its cream tile, 1254px
#   tile-{16,32,48}.png    hand-tuned renders at the exact favicon sizes
#
# The three small PNGs go into the .ico verbatim rather than being downscaled
# from the master: at 16px a hand-tuned render beats any resampler.
#
# Every image operation runs in the container. This box has neither
# ImageMagick nor librsvg and does not need them.
#
# This replaced a drawn mark -- a flat calendar page with lines of prose. That
# version was sharper at 16px, being grid-snapped geometry, but thin next to
# this one everywhere above 16. On a HiDPI display the browser takes the 32px
# frame anyway, which is where this wins.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT=$(cd ../.. && pwd)
OUT="$ROOT/src/static/icons"
TILE="#FDF4E4"            # the tile's own field colour, sampled from the art

echo "==> Building the toolchain image"
docker build -q -t dailylitbits-iconkit .
mkdir -p "$OUT"

run() { docker run --rm -u "$(id -u):$(id -g)" \
          -v "$PWD/src:/src:ro" -v "$OUT:/out" dailylitbits-iconkit "$@"; }

echo "==> favicon.ico, from the hand-tuned renders"
run convert /src/tile-16.png /src/tile-32.png /src/tile-48.png /out/favicon.ico

echo "==> manifest icons, from the master"
run sh -c 'for s in 192 512; do convert /src/tile-master.webp -resize ${s}x${s} /out/icon-$s.png; done'

echo "==> apple-touch-icon, flattened onto the tile colour"
# iOS composites transparency onto black and then rounds it itself, so the
# tile's transparent corners would show as dark wedges inside that rounding.
run convert /src/tile-master.webp -resize 180x180 \
      -background "$TILE" -alpha remove -alpha off /out/apple-touch-icon.png

echo "==> maskable icon, inset into the safe zone"
# A maskable icon is cropped to a circle 80% of the width. The field bleeds to
# the edge in the tile's own colour, so the tile's rounded edge disappears into
# it and only the drawing has to stay inside the circle.
run convert /src/tile-master.webp -resize 340x340 \
      -background "$TILE" -gravity center -extent 512x512 \
      -alpha remove -alpha off /out/icon-maskable-512.png

echo "==> checking the maskable icon against its safe zone"
# What must stay inside the circle is the drawing, not the field: anything
# that differs from the field colour counts as content.
escaped=$(run sh -c '
  convert -size 512x512 xc:black -fill white -draw "circle 256,256 256,51.2" -negate /tmp/outside.png
  convert /out/icon-maskable-512.png \( +clone -fill "'"$TILE"'" -colorize 100 \) \
          -compose difference -composite -colorspace Gray -threshold 12% /tmp/content.png
  convert /tmp/content.png /tmp/outside.png -compose multiply -composite -format "%[fx:int(maxima*255)]" info:')
if [[ "$escaped" -gt 8 ]]; then
  echo "FAILED: the drawing escapes the maskable safe zone (max outside: $escaped)" >&2
  exit 1
fi
echo "    ok — nothing outside the safe circle"

echo "==> favicon.svg is gone: the artwork is raster now"
rm -f "$OUT/favicon.svg"

echo "Done. Wrote the icon set to src/static/icons/"
