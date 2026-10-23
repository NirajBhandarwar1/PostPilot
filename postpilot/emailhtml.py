"""HTML versions of PostPilot's emails (Gmail-safe: inline styles only, no scripts or images).

The plain-text email stays the source of truth; these functions only dress it up:
- text_email(): the ideas and options emails (numbered items, "Label: text" lines, separators)
- markdown_email(): the BUILD.md plan (headings, lists, checkboxes, tables, code blocks)
"""

import html
import re

FONT = "font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif"
WRAP = f'<div style="{FONT};max-width:700px;margin:0 auto;color:#1f2328;font-size:15px;line-height:1.55">'
TAG_COLORS = {"LOOK WHAT I BUILT": "#1a7f37", "PUBLIC GITHUB REPO": "#0969da", "NEW MODEL": "#8250df",
              "VIRAL GITHUB REPO": "#0969da", "NEW PRODUCT": "#bf3989", "NEW RESEARCH": "#9a6700"}
URL = re.compile(r"(https?://[^\s<>\"')\]]+)")


def _inline(text: str) -> str:
    """Escape, then make links clickable, `code` monospace and **bold** bold."""
    t = html.escape(text, quote=False)
    t = URL.sub(lambda m: f'<a href="{m.group(1)}" style="color:#0969da">{_short_url(m.group(1))}</a>', t)
    t = re.sub(r"`([^`]+)`", r'<code style="background:#f6f8fa;padding:1px 4px;border-radius:4px;'
                             r'font-size:13px">\1</code>', t)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)


def _short_url(u: str) -> str:
    """Long redirect links shown as their site name."""
    return re.sub(r"^https?://(www\.)?", "", u).split("/")[0] + "/…" if len(u) > 70 else u


def _badge(label: str) -> str:
    color = TAG_COLORS.get(label, "#57606a")
    return (f'<span style="background:{color};color:#fff;border-radius:10px;padding:2px 8px;'
            f'font-size:11px;font-weight:600;letter-spacing:.3px;margin-left:6px">{html.escape(label)}</span>')


def text_email(text: str) -> str:
    """Ideas / options email -> HTML."""
    lines = text.splitlines()
    out = [WRAP]
    if lines:
        out.append(f'<h2 style="margin:0 0 8px;font-size:20px">{_inline(lines[0])}</h2>')
    para: list[str] = []
    label_item: list[str] | None = None

    def flush():
        nonlocal para, label_item
        if label_item:
            k, v = label_item
            out.append(f'<p style="margin:4px 0 4px 18px"><b>{html.escape(k)}:</b> {_inline(v)}</p>')
            label_item = None
        if para:
            out.append(f'<p style="margin:6px 0">{_inline(" ".join(para))}</p>')
            para = []

    for raw in lines[1:]:
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush()
            continue
        if re.fullmatch(r"[-=]{10,}", s):
            flush()
            out.append('<hr style="border:none;border-top:1px solid #d0d7de;margin:16px 0">')
            continue
        m = re.match(r"^(\d+)\.\s+(.*?)((?:\s{2,}\[[^\]]+\])*)(\s+★ MY PICK)?$", s)
        if m and not line.startswith(" "):
            flush()
            tags = "".join(_badge(t) for t in re.findall(r"\[([^\]]+)\]", m.group(3) or ""))
            pick = (' <span style="color:#bf8700;font-weight:700;font-size:13px">★ MY PICK</span>'
                    if m.group(4) else "")
            out.append(f'<h3 style="margin:10px 0 6px;font-size:17px">{m.group(1)}. {_inline(m.group(2))}'
                       f"{tags}{pick}</h3>")
            continue
        if re.fullmatch(r"[A-Z][A-Z' ]{3,}", s):                 # THE STORY, QUICK VIEW
            flush()
            out.append(f'<h4 style="margin:12px 0 4px;font-size:13px;color:#57606a;letter-spacing:.5px">{s}</h4>')
            continue
        lm = re.match(r"^\s{2,4}([A-Z][A-Za-z' ]{1,30}):\s+(.*)$", line)
        if lm:
            flush()
            label_item = [lm.group(1), lm.group(2)]
            continue
        if label_item and line.startswith("     "):               # wrapped continuation of a label line
            label_item[1] += " " + s
            continue
        flush() if label_item else None
        para.append(s)
    flush()
    out.append('<p style="color:#8c959f;font-size:12px;margin-top:18px">Reply to this email with a number. '
               "PostPilot never posts for you.</p></div>")
    return "\n".join(out)


def markdown_email(intro: str, md: str) -> str:
    """BUILD.md email: a short intro (plain text) + the plan rendered from its markdown."""
    out = [WRAP, *[f'<p style="margin:6px 0">{_inline(p)}</p>' for p in intro.strip().split("\n") if p.strip()],
           '<hr style="border:none;border-top:2px solid #d0d7de;margin:18px 0">']
    lines, i, lst = md.splitlines(), 0, None

    def close_list():
        nonlocal lst
        if lst:
            out.append(f"</{lst}>")
            lst = None

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            close_list()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            code = html.escape("\n".join(lines[i + 1:j]))
            out.append(f'<pre style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px;'
                       f'font-size:13px;white-space:pre-wrap;overflow-x:auto">{code}</pre>')
            i = j + 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip()):
            close_list()
            rows = [s]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(lines[j].strip())
                j += 1
            cell = 'style="border:1px solid #d0d7de;padding:4px 8px;text-align:left"'
            trs = []
            for k, r in enumerate(rows):
                tag = "th" if k == 0 else "td"
                cells = "".join(f"<{tag} {cell}>{_inline(c.strip())}</{tag}>" for c in r.strip("|").split("|"))
                trs.append(f"<tr>{cells}</tr>")
            out.append(f'<table style="border-collapse:collapse;font-size:13px;margin:8px 0">{"".join(trs)}</table>')
            i = j
            continue
        h = re.match(r"^(#{1,4})\s+(.*)$", s)
        if h:
            close_list()
            size = {1: 21, 2: 18, 3: 16, 4: 15}[len(h.group(1))]
            out.append(f'<h{len(h.group(1))} style="font-size:{size}px;margin:16px 0 6px">{_inline(h.group(2))}'
                       f"</h{len(h.group(1))}>")
        elif s == "---":
            close_list()
            out.append('<hr style="border:none;border-top:1px solid #d0d7de;margin:16px 0">')
        elif re.match(r"^[-*]\s+\[[ x]\]\s+", s):
            if lst != "ul":
                close_list()
                out.append('<ul style="list-style:none;padding-left:8px;margin:4px 0">')
                lst = "ul"
            out.append(f"<li>☐ {_inline(re.sub(r'^[-*]\s+\[[ x]\]\s+', '', s))}</li>")
        elif re.match(r"^[-*]\s+", s):
            if lst != "ul":
                close_list()
                out.append('<ul style="margin:4px 0;padding-left:22px">')
                lst = "ul"
            out.append(f"<li>{_inline(s[2:])}</li>")
        elif re.match(r"^\d+\.\s+", s):
            if lst != "ol":
                close_list()
                out.append('<ol style="margin:4px 0;padding-left:22px">')
                lst = "ol"
            out.append(f"<li>{_inline(re.sub(r'^\d+\.\s+', '', s))}</li>")
        elif s.startswith(">"):
            close_list()
            out.append(f'<blockquote style="border-left:3px solid #d0d7de;margin:6px 0;padding:2px 10px;'
                       f'color:#57606a">{_inline(s.lstrip("> "))}</blockquote>')
        elif s:
            close_list()
            out.append(f'<p style="margin:6px 0">{_inline(s)}</p>')
        else:
            close_list()
        i += 1
    close_list()
    out.append("</div>")
    return "\n".join(out)
