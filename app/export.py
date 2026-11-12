"""Convert the app's sanitized HTML into Markdown, a minimal .docx, or a standalone HTML file.

The .docx is written directly as WordprocessingML (no extra dependency). Lists are rendered
as indented paragraphs with bullet/number prefixes and headings use direct formatting.
"""
import io
import re
import zipfile
from html import escape
from html.parser import HTMLParser
from xml.sax.saxutils import escape as xesc

BLOCKS = {"p", "h1", "h2", "h3", "h4", "blockquote"}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class _Parser(HTMLParser):
    """Flatten HTML into blocks: {"kind": p|h1-h4|ul|ol|blockquote, "n": int, "runs": [(text,b,i,u)]}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.cur, self.lists = [], None, []
        self.f = {"b": 0, "i": 0, "u": 0}

    def _open(self, kind, n=0):
        self._close()
        self.cur = {"kind": kind, "n": n, "runs": []}

    def _close(self):
        if self.cur is not None:
            if all(r[0] == "\n" for r in self.cur["runs"]):   # Quill's blank line: <p><br></p>
                self.cur["runs"] = []
            self.blocks.append(self.cur)
            self.cur = None

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self._close()
            self.lists.append([tag, 0])
        elif tag == "li":
            if self.lists:
                self.lists[-1][1] += 1
                self._open(self.lists[-1][0], self.lists[-1][1])
            else:
                self._open("ul")
        elif tag in BLOCKS:
            self._open(tag)
        elif tag in ("strong", "b"):
            self.f["b"] += 1
        elif tag in ("em", "i"):
            self.f["i"] += 1
        elif tag == "u":
            self.f["u"] += 1
        elif tag == "br" and self.cur is not None:
            self.cur["runs"].append(("\n", False, False, False))

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self._close()
            if self.lists:
                self.lists.pop()
        elif tag == "li" or tag in BLOCKS:
            self._close()
        elif tag in ("strong", "b"):
            self.f["b"] = max(0, self.f["b"] - 1)
        elif tag in ("em", "i"):
            self.f["i"] = max(0, self.f["i"] - 1)
        elif tag == "u":
            self.f["u"] = max(0, self.f["u"] - 1)

    def handle_data(self, data):
        if self.cur is None:
            if not data.strip():
                return
            self._open("p")
        self.cur["runs"].append((data, bool(self.f["b"]), bool(self.f["i"]), bool(self.f["u"])))


def parse(html: str):
    p = _Parser()
    p.feed(html or "")
    p.close()
    p._close()
    return p.blocks


# ---- Markdown ----
def _md_run(t, b, i, u):
    if t == "\n":
        return "  \n"
    if not t.strip():
        return t
    lead, trail, core = t[:len(t) - len(t.lstrip())], t[len(t.rstrip()):], t.strip()
    if u:
        core = f"<u>{core}</u>"
    if i:
        core = f"*{core}*"
    if b:
        core = f"**{core}**"
    return lead + core + trail


def to_markdown(html: str) -> str:
    out, prev = [], None
    for blk in parse(html):
        k = blk["kind"]
        text = "".join(_md_run(*r) for r in blk["runs"]).strip()
        if not text and k == "p":
            continue
        if k[0] == "h":
            line = "#" * int(k[1]) + " " + text
        elif k == "ul":
            line = "- " + text
        elif k == "ol":
            line = f"{blk['n']}. {text}"
        elif k == "blockquote":
            line = "> " + text
        else:
            line = text
        sep = "\n" if (k == prev and k in ("ul", "ol")) else "\n\n"
        out.append(sep + line if out else line)
        prev = k
    return "".join(out) + "\n"


# ---- Word (.docx) ----
_SZ = {"h1": 40, "h2": 32, "h3": 28, "h4": 24}   # half-points


def _run(t, b=False, i=False, u=False, sz=None):
    if t == "\n":
        return "<w:r><w:br/></w:r>"
    pr = ("<w:b/>" if b else "") + ("<w:i/>" if i else "") + \
         (f'<w:sz w:val="{sz}"/>' if sz else "") + ('<w:u w:val="single"/>' if u else "")
    return f'<w:r><w:rPr>{pr}</w:rPr><w:t xml:space="preserve">{xesc(_CTRL.sub("", t))}</w:t></w:r>'


def to_docx(html: str) -> bytes:
    paras = []
    for blk in parse(html):
        k, ppr, pre = blk["kind"], "", ""
        if k in _SZ:
            ppr = '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/></w:pPr>'
        elif k in ("ul", "ol"):
            ppr = '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>'
            pre = _run("\u2022 " if k == "ul" else f"{blk['n']}. ")
        elif k == "blockquote":
            ppr = '<w:pPr><w:ind w:left="720"/></w:pPr>'
        body = "".join(_run(t, b or k in _SZ, i or k == "blockquote", u, _SZ.get(k))
                       for t, b, i, u in blk["runs"])
        paras.append(f"<w:p>{ppr}{pre}{body}</w:p>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(paras) or "<w:p/>"}</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


# ---- HTML ----
def to_html(title: str, html: str) -> str:
    return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><title>' + escape(title) +
            '</title><style>body{font:17px/1.6 Georgia,serif;max-width:72ch;margin:40px auto;'
            'padding:0 16px;color:#222}</style></head><body>\n' + (html or "") + '\n</body></html>\n')
