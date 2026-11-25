"""The plain-text half of every email, made from its HTML.

Every message goes out as multipart/alternative: this text first, the HTML
second. Spam filters treat HTML-only mail as a weak negative signal, and
plain-text readers and screen readers get something readable instead of a
wall of markup.

What it does: drops <head>, <style>, <script> and anything styled
display:none; turns block elements and <br> into line breaks; writes each
link as "text (url)" so it still works; collapses runs of blank lines.
It is not a general converter, only good enough for the app's own templates.
"""
import html
import re
from html.parser import HTMLParser

SKIP = {"head", "style", "script", "title"}
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table",
         "blockquote", "ul", "ol", "section", "header", "footer", "body"}
VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr", "source"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []      # (tag, hides) for open non-void elements
        self.hidden = 0      # depth of hiding elements we are inside
        self.links = []      # (href, index into out where its text starts)

    def _hides(self, tag, attrs):
        if tag in SKIP:
            return True
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        return "display:none" in style

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            self.handle_startendtag(tag, attrs)
            return
        hides = self._hides(tag, attrs)
        self.stack.append((tag, hides))
        if hides:
            self.hidden += 1
            return
        if self.hidden:
            return
        if tag in BLOCK:
            self.out.append("\n\n" if tag in ("p", "h1", "h2", "h3", "blockquote") else "\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self.links.append((dict(attrs).get("href"), len(self.out)))

    def handle_startendtag(self, tag, attrs):
        if self.hidden:
            return
        if tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n----\n\n")

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        # Pop to the matching element; tolerate sloppy nesting.
        while self.stack:
            open_tag, hides = self.stack.pop()
            if hides:
                self.hidden -= 1
            if open_tag == tag:
                break
        if self.hidden:
            return
        if tag == "a" and self.links:
            href, start = self.links.pop()
            label = re.sub(r"\s+", " ", "".join(self.out[start:])).strip()
            self.out[start:] = [label]
            if href and href.startswith(("http://", "https://", "mailto:")) and href != label:
                self.out.append(f" ({href})")
        if tag in BLOCK:
            self.out.append("\n\n" if tag in ("p", "h1", "h2", "h3", "blockquote") else "\n")

    def handle_data(self, data):
        # Source line breaks are layout, not text: real breaks come from tags.
        if not self.hidden:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(markup):
    parser = _Text()
    parser.feed(markup)
    parser.close()
    text = html.unescape("".join(parser.out)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
