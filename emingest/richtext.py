# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Rich text: a small WYSIWYG editor widget (ipyvue template: bold, italic, underline, headings, lists, links,
tables pasted from elsewhere, images pasted as data URIs), HTML sanitising and HTML -> plain text.

The HTML of the editor goes into the NOMAD ELN (``RichTextEditQuantity``, which NOMAD shows and edits with its own
rich-text editor) after :func:`sanitize`; NXem gets :func:`to_text`.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup", "h1", "h2", "h3", "h4", "ul",
                "ol", "li", "a", "blockquote", "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "img",
                "span", "div", "hr", "font"}
ALLOWED_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}, "td": {"colspan", "rowspan"},
                 "th": {"colspan", "rowspan"}, "span": {"style"}, "div": {"style"}, "p": {"style"},
                 "font": {"color"}}
VOID = {"br", "img", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "head", "title", "noscript", "template"}
MAX_IMAGE_BYTES = 3_000_000
_STYLE_OK = re.compile(r"^\s*(color|background-color|text-align|font-weight|font-style|text-decoration)\s*:"
                       r"\s*[#\w\s(),.%-]+$", re.I)


def _safe_url(u, img=False):
    u = (u or "").strip()
    lu = u.lower()
    if lu.startswith(("http://", "https://", "mailto:", "#", "/")) or (not re.match(r"^[a-z][a-z0-9+.-]*:", lu)):
        return u
    if img and lu.startswith("data:image/") and len(u) <= MAX_IMAGE_BYTES * 4 // 3 + 100:
        return u
    return None


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.drop = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop += 1
            return
        if self.drop or tag not in ALLOWED_TAGS:
            return
        keep = []
        for k, v in attrs:
            k = k.lower()
            if k not in ALLOWED_ATTRS.get(tag, ()):
                continue
            if k in ("href", "src"):
                v = _safe_url(v, img=(tag == "img"))
                if v is None:
                    continue
            if k == "style":
                v = ";".join(p for p in (v or "").split(";") if _STYLE_OK.match(p))
                if not v:
                    continue
            keep.append(f' {k}="{_html.escape(v or "", quote=True)}"')
        if tag == "a":
            keep.append(' target="_blank" rel="noopener"')
        self.out.append(f"<{tag}{''.join(keep)}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag not in ALLOWED_TAGS or tag in VOID:
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(_html.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def sanitize(html):
    """Allow-list sanitising (no scripts, event handlers, javascript: URLs, frames, forms)."""
    if not html:
        return ""
    s = _Sanitizer()
    s.feed(str(html))
    s.close()
    out = "".join(s.out).strip()
    return "" if out in ("<br>", "<p></p>", "<div></div>", "<p><br></p>", "<div><br></div>") else out


class _Text(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "blockquote", "pre", "table", "ul", "ol", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.drop = 0
        self.lists = []
        self.href = None

    def nl(self, n=1):
        """End the current line and make sure there are n line breaks (n=2: a blank line) before what follows."""
        tail = "".join(self.out[-4:])
        if not tail.strip() and not "".join(self.out).strip():
            return
        have = len(tail) - len(tail.rstrip("\n"))
        if have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.drop += 1
            return
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4"):
            self.nl(2)
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self.nl(1 if len(self.lists) > 1 else 2)
        elif tag == "li":
            self.nl()
            if self.lists:
                self.lists[-1][1] += 1
                lv = self.lists[-1]
                self.out.append("  " * (len(self.lists) - 1) + (f"{lv[1]}. " if lv[0] == "ol" else "- "))
            else:
                self.out.append("- ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "img":
            self.out.append(f"[image: {a.get('alt') or 'embedded'}]")
        elif tag == "a":
            self.href = a.get("href")
        elif tag in self.BLOCK:
            self.nl(2 if tag in ("p", "table", "blockquote", "pre") else 1)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.nl(2 if not self.lists else 1)
        elif tag == "a":
            if self.href and not self.href.startswith("#"):
                self.out.append(f" <{self.href}>")
            self.href = None
        elif tag in self.BLOCK:
            self.nl(2 if tag in ("p", "h1", "h2", "h3", "h4", "table", "blockquote", "pre") else 1)

    def handle_data(self, data):
        if not self.drop:
            self.out.append(re.sub(r"\s+", " ", data))


def to_text(html):
    """Plain text (Markdown-like: headings, lists, links) of the HTML."""
    if not html:
        return ""
    if "<" not in str(html):
        return str(html).strip()
    p = _Text()
    p.feed(str(html))
    p.close()
    txt = "".join(p.out)
    txt = "\n".join(line.strip() if not line.lstrip().startswith(("- ", "  ")) else line.rstrip()
                    for line in txt.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def from_text(text):
    """HTML of plain text / simple Markdown (headings, lists, paragraphs)."""
    out, lst = [], None
    for line in str(text or "").splitlines():
        s = line.strip()
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        li = re.match(r"^([-*]|\d+\.)\s+(.*)$", s)
        if lst and not li:
            out.append(f"</{lst}>")
            lst = None
        if m:
            out.append(f"<h{len(m.group(1))}>{_html.escape(m.group(2))}</h{len(m.group(1))}>")
        elif li:
            t = "ol" if li.group(1)[0].isdigit() else "ul"
            if lst != t:
                if lst:
                    out.append(f"</{lst}>")
                out.append(f"<{t}>")
                lst = t
            out.append(f"<li>{_html.escape(li.group(2))}</li>")
        elif s:
            out.append(f"<p>{_html.escape(s)}</p>")
    if lst:
        out.append(f"</{lst}>")
    return "".join(out)


TEMPLATE = r"""
<template>
  <div class="tem-rte" :style="'max-width:' + width">
    <div class="tem-rte-bar">
      <button title="Bold (Ctrl+B)" @mousedown.prevent="cmd('bold')"><b>B</b></button>
      <button title="Italic (Ctrl+I)" @mousedown.prevent="cmd('italic')"><i>I</i></button>
      <button title="Underline (Ctrl+U)" @mousedown.prevent="cmd('underline')"><u>U</u></button>
      <button title="Strike through" @mousedown.prevent="cmd('strikeThrough')"><s>S</s></button>
      <button title="Subscript" @mousedown.prevent="cmd('subscript')">x<sub>2</sub></button>
      <button title="Superscript" @mousedown.prevent="cmd('superscript')">x<sup>2</sup></button>
      <span class="sep"></span>
      <button title="Heading" @mousedown.prevent="cmd('formatBlock', 'h3')">H</button>
      <button title="Paragraph" @mousedown.prevent="cmd('formatBlock', 'p')">&#182;</button>
      <button title="Bulleted list" @mousedown.prevent="cmd('insertUnorderedList')">&#8226; list</button>
      <button title="Numbered list" @mousedown.prevent="cmd('insertOrderedList')">1. list</button>
      <button title="Quote" @mousedown.prevent="cmd('formatBlock', 'blockquote')">&#8220;&#8221;</button>
      <span class="sep"></span>
      <button title="Link: select text, then enter the URL in the field" @mousedown.prevent="link()">link</button>
      <input class="tem-rte-url" v-model="url" placeholder="https://..." @keydown.enter.prevent="link()" />
      <button title="Remove formatting" @mousedown.prevent="cmd('removeFormat')">clear fmt</button>
      <button title="Undo" @mousedown.prevent="cmd('undo')">&#8630;</button>
      <button title="Redo" @mousedown.prevent="cmd('redo')">&#8631;</button>
    </div>
    <div ref="ed" class="tem-rte-ed" contenteditable="true" :data-ph="placeholder"
         :style="'min-height:' + height" @input="sync" @blur="sync" @keyup="saveSel" @mouseup="saveSel"></div>
  </div>
</template>
<script>
module.exports = {
  data() { return { url: "", sel: null }; },
  mounted() { this.$refs.ed.innerHTML = this.value || ""; },
  watch: {
    value(v) { const ed = this.$refs.ed; if (ed && ed.innerHTML !== v) ed.innerHTML = v || ""; }
  },
  methods: {
    sync() { const h = this.$refs.ed.innerHTML; if (h !== this.value) this.value = h; },
    saveSel() {
      const s = window.getSelection();
      if (s && s.rangeCount && this.$refs.ed.contains(s.anchorNode)) this.sel = s.getRangeAt(0).cloneRange();
    },
    restoreSel() {
      if (!this.sel) return;
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(this.sel);
    },
    cmd(c, a) {
      this.$refs.ed.focus(); this.restoreSel();
      document.execCommand(c, false, a || null); this.saveSel(); this.sync();
    },
    link() {
      let u = (this.url || "").trim();
      if (!u) return;
      if (!/^(https?:|mailto:)/i.test(u)) u = "https://" + u;
      this.$refs.ed.focus(); this.restoreSel();
      const s = window.getSelection();
      if (s && s.isCollapsed) document.execCommand("insertText", false, u);
      document.execCommand("createLink", false, u);
      this.url = ""; this.sync();
    }
  }
};
</script>
<style id="tem-rte-style">
.tem-rte { border: 1px solid #bbb; border-radius: 4px; background: #fff; }
.tem-rte-bar { display: flex; flex-wrap: wrap; gap: 2px; align-items: center; padding: 3px;
               border-bottom: 1px solid #ddd; background: #f6f6f6; }
.tem-rte-bar button { border: 1px solid #ccc; background: #fff; border-radius: 3px; padding: 1px 7px;
                      font-size: 13px; cursor: pointer; min-width: 26px; color: #222; }
.tem-rte-bar button:hover { background: #e8eefc; }
.tem-rte-bar .sep { width: 8px; }
.tem-rte-url { border: 1px solid #ccc; border-radius: 3px; padding: 1px 4px; font-size: 12px; width: 160px; }
.tem-rte-ed { padding: 6px 10px; outline: none; overflow-y: auto; max-height: 420px; font-size: 14px;
              line-height: 1.45; color: #111; }
.tem-rte-ed:empty:before { content: attr(data-ph); color: #999; }
.tem-rte-ed h3 { font-size: 1.1em; margin: 6px 0 2px; }
.tem-rte-ed p { margin: 0 0 6px; }
.tem-rte-ed ul, .tem-rte-ed ol { padding-left: 22px; margin: 2px 0 6px; }
.tem-rte-ed blockquote { border-left: 3px solid #bbb; margin: 4px 0; padding-left: 8px; color: #444; }
.tem-rte-ed table { border-collapse: collapse; } .tem-rte-ed td, .tem-rte-ed th { border: 1px solid #ccc; padding: 2px 5px; }
.tem-rte-ed img { max-width: 100%; }
</style>
"""


def RichTextEditor(value="", placeholder="", height="140px", width="900px"):
    """The editor widget; ``.value`` is its HTML (synced on every change).  Needs ipyvue (comes with ipyvuetify /
    Solara); without it a Textarea (plain text / simple Markdown) is returned."""
    try:
        import ipyvue as vue
        import traitlets as tl

        class _RTE(vue.VueTemplate):
            template = tl.Unicode(TEMPLATE).tag(sync=True)
            value = tl.Unicode("").tag(sync=True)
            placeholder = tl.Unicode("").tag(sync=True)
            height = tl.Unicode("140px").tag(sync=True)
            width = tl.Unicode("900px").tag(sync=True)

        return _RTE(value=value or "", placeholder=placeholder, height=height, width=width)
    except Exception:  # noqa: BLE001
        import ipywidgets as W
        return W.Textarea(value=to_text(value), placeholder=placeholder,
                          layout=W.Layout(width=width, height=height))
