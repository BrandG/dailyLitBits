# The DailyLitBits mark: a calendar page carrying lines of prose rather than
# dates. The two binding rings are what make it read as a calendar instead of
# a generic document -- without them it is just a card with text on it. The
# lines are what make it reading rather than scheduling. One object, not a
# book sitting next to a calendar, because two objects in 256 pixels is mud.
#
# Colours are the site's own custom properties: --dlb-accent for the field,
# --dlb-accent-hover for the header band, --dlb-paper for the page.
#
# Every edge sits on a multiple of 4. The smallest render is 16px, a 4:1
# reduction of this 64-unit artwork, so grid-aligned edges land on whole
# pixels. Off the grid the whole thing turns to mush at 16px -- the first
# draft had the card at x=14 and it was visibly soft.
import sys

FIELD, HEAD, PAPER, TEXT = "#1f6b4f", "#185740", "#fbf8f3", "#8d8378"

# A maskable icon is cropped to a circle 80% of the width. The card's corners
# sit about 31 from centre against a safe radius of 25.6, so the artwork is
# scaled down for that variant while the field still bleeds to the edge.
scale = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
def s(v): return 32 + (v - 32) * scale

out = [f'<rect width="64" height="64" fill="{FIELD}"/>']
def rect(x, y, w, h, f):
    out.append(f'<rect x="{s(x):g}" y="{s(y):g}" width="{w*scale:g}" height="{h*scale:g}" fill="{f}"/>')

rect(20, 8, 4, 8, PAPER)      # binding rings
rect(40, 8, 4, 8, PAPER)
rect(12, 16, 40, 36, PAPER)   # the page
rect(12, 16, 40, 8, HEAD)     # header band
rect(20, 28, 24, 4, TEXT)     # lines of prose
rect(20, 36, 24, 4, TEXT)
rect(20, 44, 16, 4, TEXT)

print(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" '
      f'role="img" aria-label="DailyLitBits">{"".join(out)}</svg>')
