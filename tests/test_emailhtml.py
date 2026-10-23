"""HTML emails: safe (escaped), readable (badges, labels), links clickable."""

from postpilot import emailhtml

IDEAS = """PostPilot #9: what's hot in AI, 23 Oct 2026
Reply with a number (1-2) to get build options for it, or 'skip'.

------------------------------------------------------------
1. DeepSeek Flash vs GPT-6 on real bugs   [NEW MODEL]   ★ MY PICK
   The story: DeepSeek shipped a <fast> model & it's cheap. It is wrapped
     onto a second line here.
   Effort: easy · 60 min · about $0.14
   Source: https://news.google.com/rss/articles/CBMiywFBVV95cUxPaXFBckZ0NnY0WmJlNE12RXNpWFJKVUs2MmxaeTNZY2otWWRPMFNzeFlO
2. Can rea rebuild it?   [VIRAL GITHUB REPO]
   What you'd do: Clone `morluto/rea`.
------------------------------------------------------------
Why I pick #1: hot"""


def test_ideas_email_html():
    h = emailhtml.text_email(IDEAS)
    assert "<h2" in h and "DeepSeek Flash vs GPT-6 on real bugs" in h
    assert ">NEW MODEL</span>" in h and "★ MY PICK" in h and ">VIRAL GITHUB REPO</span>" in h
    assert "<b>The story:</b> DeepSeek shipped a &lt;fast&gt; model &amp; it's cheap. It is wrapped onto a second line here." in h
    assert 'href="https://news.google.com/rss/articles/CBMi' in h and ">news.google.com/…</a>" in h   # long link shortened
    assert "<code" in h and "<script" not in h


def test_build_email_html():
    md = """# BUILD: X
## Before you start (🧑 you, once)
- [ ] Install Node
### Step 1 🤖: Create the project  (~10 min)
```powershell
git init
echo "<b>"
```
| model id | base_url |
| --- | --- |
| `gpt-6-sol` | `https://api.openai.com/v1` |
1. First shot
2. Second shot
> **Note from PostPilot:** check it."""
    h = emailhtml.markdown_email("Plan ready.\nPaste it into Cursor.", md)
    assert "<h1" in h and "<h3" in h and "☐ Install Node" in h
    assert "<pre" in h and "&lt;b&gt;" in h                               # code is escaped, not run
    assert "<table" in h and "<th " in h and "gpt-6-sol" in h
    assert "<ol" in h and "<li>Second shot</li>" in h and "<blockquote" in h and "<b>Note from PostPilot:</b>" in h
