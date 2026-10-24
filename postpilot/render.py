"""Everything PostPilot shows you: the three emails (plain text + HTML) and the BUILD.md plan.

Layout only: no model calls, no email sending, no database writes. pipeline.py decides what to
say; this module decides how it looks."""

import html
import re
import textwrap
import unicodedata
from datetime import date

from . import brain, factgate, llm


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


HANDLE_LABEL = {"model": "New model", "github_repo": "Viral GitHub repo", "product": "New product",
                "paper": "New research", "news": "News"}


def _cost_text(i: dict) -> str:
    c = i.get("est_cost")
    if not i.get("models_to_run"):
        return "free (no model calls)"
    if c is None:
        return "unknown (a paid model has no listed price)"
    return "free (only free models)" if c == 0 else f"about ${c:.2f} (20 prompts per model)"


def _pretty_where(where: str) -> str:
    """'News x8 | Hacker News x2 | GitHub trending: +129,417 stars this week' in plain words."""
    parts = []
    for p in (where or "").split(" | "):
        m = re.fullmatch(r"(.+?) x(\d+)", p.strip())
        if m:
            name, n = m.group(1), int(m.group(2))
            parts.append(f"{n} news articles" if name == "News" else f"{name} ({n} posts)")
        elif p.strip().startswith("News:"):
            parts.append("in the news")
        elif p.strip():
            parts.append(p.strip().replace("Hugging Face trending models: HF trending", "Hugging Face trending"))
    return " · ".join(parts)


def _best_sources(urls: list[str], n: int = 2) -> list[str]:
    """Readable links first; Google News redirect links only if nothing else."""
    good = [u for u in urls if "news.google.com" not in u]
    return (good or urls)[:n]


def render_ideas(batch_id: int, batch: dict) -> str:
    """The first email: each hot story as one clear idea, enough to choose from on a phone."""
    ideas = batch["ideas"]
    out = [f"PostPilot #{batch_id}: what's hot in AI, {date.today():%d %b %Y}",
           f"Reply with a number (1-{len(ideas)}) to get build options for it, or 'skip'.",
           "Shortcut: reply '2 go' to skip the options and get my top build plan right away.", ""]
    if batch.get("short"):
        st = batch["stats"]
        out += [f"SHORT BATCH: only {len(ideas)} idea passed the fact check "
                f"({st['dropped']} dropped after a retry).", ""]
    for n, i in enumerate(ideas, 1):
        label = HANDLE_LABEL.get(i.get("handle_type", "news"), "News").upper()
        pick = "   ★ MY PICK" if n == batch.get("recommended") else ""
        out += ["-" * 60, f"{n}. {i.get('short_title') or i['title']}   [{label}]{pick}"]
        out += _wrap("The story", i.get("the_news") or i["why_hot"])
        out += _wrap("Why it's hot", _pretty_where(i.get("heat_where", "")))
        out += _wrap("What you'd do", i.get("what_you_do") or i["build_plan"])
        out += _wrap("What the post shows", i.get("post_shows") or i["visual"])
        out += _wrap("Why it gets you hired", i.get("why_hired") or i["role_angle"])
        out.append(f"   Effort: {i.get('difficulty', 'medium')} · {_hm(int(i.get('build_minutes') or 60)).lstrip('~')}"
                   f" · {_cost_text(i).split(' (')[0]}")
        if i.get("needs"):
            out += _wrap("You need", ", ".join(i["needs"]))
        if i.get("check_before_posting"):
            out += _wrap("Check first", "; ".join(i["check_before_posting"]))
        out += [f"   Source: {u}" for u in _best_sources(i.get("source_urls", []))]
        out.append("")
    if batch.get("also_hot"):
        out += ["-" * 60, *textwrap.wrap("Also hot (not picked): " + ", ".join(batch["also_hot"]), 88)]
    out += ["-" * 60, f"Why I pick #{batch['recommended']}: {batch['recommended_reason']}",
            "", "Optional: rate the ideas 1-5 to teach me your taste, e.g. '2 rate 4 5 2'."]
    writers = batch.get("writers") or []
    primary = (llm.providers("writer") or [None])[0]
    if writers and primary and not any(w.startswith(primary.name + ":") for w in writers):
        out.append(f"Note: {primary.name} did not answer (outage or daily cap), so a backup model "
                   f"({', '.join(writers)}) wrote this batch. Expect weaker ideas.")
    return "\n".join(out)


CURSOR_PROMPT = ("Read BUILD.md. Build Part A one step at a time. Do the 🤖 steps yourself; when you reach "
                 "a 🧑 step, stop and tell me exactly what to do, then wait. After each step run its "
                 "\"Done when\" check and fix any error before moving on. Never run a line marked 🧑⚠. "
                 "Follow the Rules strictly. Stop at the end of Part A.")


# Exact connection details per provider, filled in by code (never by the model).
PROVIDER_API = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "platform.openai.com/api-keys (paid)"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "console.groq.com/keys (free)"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter.ai/keys"),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/<CLOUDFLARE_ACCOUNT_ID>/ai/v1",
                   "CLOUDFLARE_API_TOKEN", "dash.cloudflare.com > AI > Workers AI (free tier)"),
}


def _as_command(line: str) -> str:
    """A plain sentence in the setup list ("Visit sqlite.org and download ...") becomes a comment,
    so nobody pastes it into PowerShell. Cmdlets look like Verb-Noun, so "Word word" is prose."""
    s = line.strip()
    return s if s.startswith("#") or not re.match(r"^[A-Z][a-z]+\s", s) else "# " + s


TOTAL_SLACK = 1.5   # warn when the plan's steps add up to more than 1.5x the promised time
FIXED_MINUTES = {"setup": 10, "record": 15, "publish": 10}


def brief_warnings(idea: dict, brief) -> list[str]:
    """Code-level checks on the plan the model wrote."""
    out = []
    total = sum(t.minutes for t in brief.tasks) + sum(FIXED_MINUTES.values())
    promised = int(idea.get("build_minutes") or 0)
    if promised and total > TOTAL_SLACK * promised:
        out.append(f"steps add up to ~{total} min, the option promised ~{promised} min")
    kind, handle = idea.get("handle_type", ""), (idea.get("handle") or "").split("/")[-1].lower()
    if kind == "model":
        if not brief.uses_models:
            out.append(f"the plan never calls a model, but the story is about {idea.get('handle')}")
    elif handle and not any(handle in (t.details + t.title + brief.hot_thing_usage).lower() for t in brief.tasks):
        out.append(f"no step uses {idea.get('handle')}")
    if brief.uses_models and not idea.get("models_to_run"):
        out.append("the plan calls models but none were approved")
    return out


TEMPLATE_PACKAGES = {"pytest"}                      # Step 1 installs only these (plus model clients)
MODEL_PACKAGES = {"openai", "python-dotenv"}


def _requirements(brief) -> list[str]:
    """Step 1's packages: fixed by the template, never taken from the model (D3, D16)."""
    req = set(TEMPLATE_PACKAGES) | (MODEL_PACKAGES if brief.uses_models else set())
    return sorted(req, key=str.lower)


# A pip requirement: name, optional [extras], optional version pin. Anything else is dropped.
PKG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!-]+)?")


def _extra_packages(brief) -> list[str]:
    """Packages the model asked for beyond the template: installed only in the 🧑 review step.
    Only valid package names survive, so nothing else can ride along into the pip command."""
    base = {p.lower() for p in TEMPLATE_PACKAGES | MODEL_PACKAGES}
    return sorted({r.strip() for r in brief.requirements
                   if PKG_RE.fullmatch(r.strip()) and r.strip().lower() not in base}, key=str.lower)


# Text that fetches, installs or runs someone else's code: always a 🧑 step or a 🧑⚠ line, never
# Cursor's alone (D16). A list can't know every tool, so plain words like "download" count too.
THIRD_PARTY_RE = re.compile(
    r"\bgit\s+(?:clone|submodule|pull|fetch)\b|\bgh\s+(?:repo\s+clone|release\s+download)\b|"
    r"\b(?:pip[\d.]*|uv|poetry|pipx|conda|mamba|brew|apt(?:-get)?|scoop)\s+(?:install|add|run)\b|"
    r"\bpython[\d.]*(?:\.exe)?\s+-m\s+pip\s+install\b|\b(?:uvx|bunx|pnpx)\b|\bdeno\s+run\b|"
    r"\bnpm\s+(?:i|install|ci|exec|create|init)\b|\bnpx\b|\b(?:pnpm|yarn|bun)\s+(?:add|install|dlx|x|create|init)\b|"
    r"\bcurl\b|\bwget\b|\bgo\s+(?:run|install)\b|\bollama\s+(?:pull|run)\b|\bvllm\s+serve\b|"
    r"from_pretrained|trust_remote_code|torch\.hub|_download\b|"
    r"Invoke-WebRequest|Invoke-RestMethod|Invoke-Expression|Start-BitsTransfer|\b(?:iwr|irm|iex)\b|"
    r"\bcertutil\b|\bdocker\s+(?:run|compose|pull)\b|\bbash\s+\S+\.sh\b|\.ps1\b|"
    r"\b(?:make|cargo|winget|choco)\s+install\b|\b(?:huggingface-cli|hf)\s+download\b|\bgit\s+lfs\b|"
    r"\bclon(?:e|es|ed|ing)\b|"
    # other plain words count when they come with a site or a run: "download X from example.com and run it"
    r"\b(?:download|install|fetch)\w*\b.{0,80}?(?:https?://|\b[\w-]+\.(?:com|io|co|org|dev|ai|sh|net|app|so)\b|"
    r"\b(?:run|execute|launch|open|start|double-click|unzip|extract|load)\b)", re.I)


def _plain(text: str) -> str:
    """Drop invisible format characters (zero-width spaces etc.) that could split a word."""
    return "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")


def _runs_third_party(text: str, handle: str = "") -> bool:
    """True if text fetches/installs code, or runs the story's third-party repo (`handle`) itself."""
    text = _plain(text)
    if THIRD_PARTY_RE.search(text):
        return True
    name = (handle or "").split("/")[-1].lower()
    return bool(name and len(name) >= 3 and re.search(
        rf"\b(run|start|launch|execute|serve|invoke|call|import)\b.{{0,60}}\b{re.escape(name)}\b", text.lower()))


def _safe(text: str, one_line: bool = False) -> str:
    """Model text can't change the shape of BUILD.md: no code fences, no fake headings, steps,
    quotes or 'Cursor:' lines. One-line fields (commands, titles) lose their line breaks."""
    s = re.sub(r"`{3,}|~{3,}", "'''", _plain(text))
    if one_line:
        return " ".join(s.split())
    return "\n".join(re.sub(r"^(\s*)(#|>|\*\*Cursor)", r"\1\\\2", ln) for ln in s.splitlines())


HUMAN_ONLY = "🧑⚠ (you check and run this, not Cursor): "


def _guard(text: str, handle: str = "") -> str:
    """Model text Cursor reads outside the steps: lines that fetch, install or run third-party code
    are marked human-only (and Cursor's prompt says never to run them)."""
    return "\n".join(HUMAN_ONLY + ln if ln.strip() and _runs_third_party(ln, handle) else ln
                     for ln in _safe(text).splitlines())


def render_brief(idea, brief, kit, post: str, score, report, checks, plan_checks,
                 batch_id: int | None = None, build_no: int | None = None) -> str:
    """BUILD.md: Part A for Cursor (fixed first/last steps around the model's tasks), Part B for you.
    Every model-written string goes through _safe/_guard: Cursor acts on Part A (D3, D16)."""
    runs = idea.get("models_to_run", []) if brief.uses_models else []
    env = sorted({PROVIDER_API[k.split(":")[0]][1] for k in runs if k.split(":")[0] in PROVIDER_API})
    if any(k.startswith("cloudflare:") for k in runs):
        env.insert(0, "CLOUDFLARE_ACCOUNT_ID")
    opt = idea.get("option") or {}
    repo = slug(brief.project_name)
    req = _requirements(brief)
    extra_pkgs = _extra_packages(brief)
    dropped_pkgs = sum(1 for r in brief.requirements if r.strip() and not PKG_RE.fullmatch(r.strip()))
    # Running the story's own repo is third-party code; calling a model or product API is not.
    handle = (idea.get("handle") or "") if idea.get("handle_type") == "github_repo" else ""
    known_hosts = factgate.SETUP_HOSTS
    setup = [_safe(c, one_line=True) for c in brief.setup_commands if c.strip()]
    commands = [c for c in setup if not _as_command(c).startswith("#")]
    prose = [c for c in setup if _as_command(c).startswith("#")]
    g = lambda text: _guard(text, handle)            # noqa: E731  (Part A text Cursor reads)
    batch_ref = str(batch_id) if batch_id else "<batch>"
    build_flag = f" --build {build_no}" if build_no else ""
    total = sum(t.minutes for t in brief.tasks) + sum(FIXED_MINUTES.values())
    warnings = brief_warnings(idea, brief)

    one = lambda s: g(_safe(str(s or ""), one_line=True))        # noqa: E731  (header fields)
    L = [f"# BUILD: {one(idea['title'])}", "",
         f"Story: {one(idea.get('topic', ''))}  |  Type: {one(opt.get('kind', idea.get('format', '')))}  |  "
         f"Difficulty: {one(opt.get('difficulty', '?'))}  |  Realistic time: ~{_hm(total).lstrip('~')}", "",
         "## How to use this file",
         "1. Do the **Before you start** list below (5-15 minutes).",
         "2. Make an empty folder, open it in Cursor, and put this file in it as `BUILD.md`.",
         "3. In Cursor's Agent chat, paste:",
         f"   > {CURSOR_PROMPT}",
         "4. Never paste keys into the chat; put them in `.env` when asked.",
         "5. When everything works, do Part B yourself.", ""]
    writers = sorted(llm.USAGE)
    primary = (llm.providers("writer") or [None])[0]
    if writers and primary and not any(w.startswith(primary.name + ":") for w in writers):
        warnings = [*warnings, f"written by a backup model ({', '.join(writers)}) because {primary.name} "
                    "was unavailable or over today's cap: expect a weaker plan; consider rebuilding tomorrow"]
    if warnings:
        L += ["> **Note from PostPilot:** " + "; ".join(warnings) + ".", ""]
    before = []
    for x in [*brief.before_you_start, *prose]:
        x = _safe(x, one_line=True)
        warn = factgate.scan_code([("before", x)], extra_hosts=known_hosts)
        before.append(f"- [ ] {x}" + ("  ⚠ " + "; ".join(w.split(": ", 1)[-1] for w in warn) if warn else ""))
    L += ["---", "", "# PART A: Build instructions (for Cursor)", "",
          "## Goal", g(brief.goal), "", f"Repo name: `{repo}`", "",
          "## Before you start (🧑 you, once)",
          *before,
          *([f"- [ ] Get keys for `.env`: " + "; ".join(f"{v[1]} from {v[2]}" for v in PROVIDER_API.values()
                                                         if v[1] in env)] if env else []),
          *(["- [ ] Run third-party code in a sandbox, not directly on this laptop: Windows Sandbox (Windows "
              "Pro: Turn Windows features on or off > Windows Sandbox) or a dev container. Your `.env` keys "
              "and files stay out of reach."]
            if idea.get("handle_type") == "github_repo" or commands or extra_pkgs else []),
          "- [ ] Python: `py -3.12 --version` prints 3.12.x. Git: `git --version` works.", ""]
    if brief.hot_thing_usage:
        L += [f"## How {_safe(idea.get('handle') or 'the hot thing', one_line=True)} is used in this build",
              g(brief.hot_thing_usage), ""]
    L += ["## Done means", *[f"- [ ] {g(_safe(c, one_line=True))}" for c in brief.done_criteria], "",
          "## Stack", *[f"- {g(_safe(x, one_line=True))}" for x in brief.stack], "",
          "## Files", *[f"- `{_safe(f.path, one_line=True).replace('`', '')}`: {g(_safe(f.purpose, one_line=True))}"
                        for f in brief.file_tree], ""]
    if brief.visual_spec:
        L += ["## Look and feel (the demo video sells this)", g(brief.visual_spec), ""]
    if runs:
        L += ["## AI models the code may call (only these)",
              "All are OpenAI-compatible: use the `openai` package with `base_url` and the key below.", "",
              "| model id | base_url | key in .env |", "| --- | --- | --- |"]
        for k in runs:
            prov, mid = k.split(":", 1)
            base, key, _ = PROVIDER_API.get(prov, ("?", "?", "?"))
            L.append(f"| `{mid}` | `{base}` | `{key}` |")
        L.append("")
    L += ["## Sample data", g(brief.demo_data), "", "## Steps", ""]

    # Step 1 is always the same, so it is always right.
    env_lines = [f"{e}=" for e in env] + (["MAX_CALLS=60"] if runs else ["# no keys needed"])
    L += ["### Step 1 🤖: Create the project  (~10 min)",
          "Run these in the project folder (PowerShell):", "```powershell", "git init",
          "py -3.12 -m venv .venv",
          f"Set-Content requirements.txt \"{chr(96)}n\".Join(@({', '.join(repr(r) for r in req)}))".replace("'", '"'),
          ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt", "```",
          "Create `.gitignore` with: `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.db`, `node_modules/`.",
          "Create `.env.example` with:", "```", *env_lines, "```",
          "", "**Done when:** `git check-ignore .env` prints `.env`, and "
          "`.\\.venv\\Scripts\\python.exe -m pytest --version` prints a version.", "",
          f"All later steps run Python as `{brain.VENV_PY}` and tests as `{brain.TEST_CMD}`.", ""]
    n = 2
    if commands or extra_pkgs:
        # D3 + D16: everything the model wants installed or fetched waits for a human look first.
        review = [*commands]
        if extra_pkgs:      # quoted: '>=' or '<' in a pin is a redirect in PowerShell
            review.append(".\\.venv\\Scripts\\python.exe -m pip install " + " ".join(f"'{p}'" for p in extra_pkgs))
        L += [f"### Step {n} 🧑: Review, then run the third-party setup  (~5 min)",
              "These commands come from the AI's reading of public docs, not from PostPilot's template. "
              "Read each one, check the package names and sites are the official ones, then run them "
              "yourself (in your sandbox for third-party repos). Cursor must not run them for you.",
              "```powershell", *review, "```"]
        for c in review:
            warn = factgate.scan_code([("setup", c)], extra_hosts=known_hosts)
            if warn:
                L.append(f"- ⚠ `{c[:80].replace('`', '')}`: " + "; ".join(w.split(": ", 1)[-1] for w in warn))
        if dropped_pkgs:
            L.append(f"- ⚠ {dropped_pkgs} requested package name(s) were not valid and were left out.")
        L += ["", "**Done when:** you have checked and run every command above without errors.", ""]
        n += 1
    for t in brief.tasks:
        title = _safe(re.sub(r"^(?:step\s*\d+\s*[:.)-]\s*)?(?:\d+\s*[.):-]\s*)?", "", t.title, flags=re.I),
                      one_line=True)
        details, done_when = _safe(t.details), _safe(t.done_when, one_line=True)
        # The title and the "Done when" check are read and run by Cursor too: gated like the details.
        forced = _runs_third_party(f"{t.title}\n{t.details}\n{t.done_when}", handle)
        mark = "🧑" if forced or (t.who or "").lower().startswith("you") else "🤖"
        L += [f"### Step {n} {mark}: {title}  (~{t.minutes} min)", details, ""]
        if forced:
            warn = factgate.scan_code([("step", f"{details}\n{done_when}")], extra_hosts=known_hosts)
            L += ["⚠ This step fetches, installs or runs third-party code: you run it (in your sandbox), "
                  "Cursor does not." + (" Scan: " + "; ".join(w.split(": ", 1)[-1] for w in warn) if warn else ""),
                  ""]
        L += [f"**Done when:** {done_when}", ""]
        n += 1
    L += [f"### Step {n} 🧑: Record the demo and add the GIF  (~15 min)",
          "Follow the shot list in Part B. Record with Win+Alt+R (Xbox Game Bar) or ScreenToGif (free). "
          "Save a 10-15 second GIF as `docs/demo.gif`; Cursor then adds it at the top of README.md.", "",
          "**Done when:** `docs/demo.gif` exists and shows in the README preview.", "",
          f"### Step {n + 1} 🧑: Publish the repo on GitHub  (~10 min)",
          "Check nothing secret is included, then publish (you run these, not Cursor):", "```powershell",
          "git ls-files .env            # must print nothing", "git add .",
          f"git commit -m \"{repo}: first version\"",
          f"gh repo create {repo} --public --source . --push   # or create it on github.com and push", "```",
          "Add a short repo description and 3-5 topics on GitHub (e.g. the hot thing's name).", "",
          "**Done when:** the repo page shows the README with the GIF.", ""]
    L += [f"## Tests (run with `{brain.TEST_CMD}`; no network, no keys)",
          *[f"- {g(_safe(x, one_line=True))}" for x in brief.tests], "",
          "## Rules (must follow)",
          "- Never run a 🧑 step or a line marked 🧑⚠ yourself: stop and tell the person what to run.",
          "- Never print, log or commit secrets. `.env` stays in `.gitignore`.",
          *(["- Read keys only from `.env` (python-dotenv). Call only the models listed above; stop after "
             "`MAX_CALLS` calls and cache responses to disk."] if runs else
            ["- The app itself calls no AI model and needs no keys."]),
          "- Only open-source (permissive license) or self-written targets and data. No scraping against "
          "a site's terms, no copies of paid software, no personal data.",
          "- No deleting files outside this folder; do not run downloaded binaries the person has not approved.",
          "- Keep it small and working: a smaller demo that runs beats a big one that doesn't.",
          "", "## README (public repo)", *[f"- {g(_safe(x, one_line=True))}" for x in brief.readme_outline], "",
          "## Known pitfalls", *[f"- {g(_safe(x, one_line=True))}" for x in brief.pitfalls]]
    if plan_checks:
        L += ["", "## Check before building", *[f"- {g(_safe(c, one_line=True))}" for c in plan_checks]]
    flags = report.get("structures", [])
    L += ["", "**Cursor: stop here. Part B is for the human.**", "", "---", "",
          "# PART B: Demo and LinkedIn post (for you)", "",
          "## 1. Record the demo (30-60 seconds)",
          *[f"{k}. {_safe(x, one_line=True)}" for k, x in enumerate(brief.demo_script, 1)], "",
          "## 2. The post (replace every {{...}} with your real results)", "", "```", _safe(post.strip()), "```", "",
          f"Human score: {score[1]:.0f}/100 ({score[2]})",
          *(["Lines to rewrite by hand:", *[f"- {s['name']} (x{s['count']}): {s['fix']}" for s in flags]]
            if flags else []), "",
          "## 3. Fill these in", *[f"- {_safe(x, one_line=True)}" for x in kit.placeholders], "",
          "## 4. Other opening lines", *[f"- {_safe(x, one_line=True)}" for x in kit.alt_hooks], "",
          "## 5. Image / video", _safe(kit.visual_spec), "",
          "## 6. First comment (post it right after publishing)", _safe(kit.first_comment) or "{{your repo link}}", "",
          "## 7. Check before you post", *([f"- {_safe(c, one_line=True)}" for c in checks] or ["- nothing flagged"]), "",
          "## 8. After you post",
          f"- Record it: `python -m postpilot posted {batch_ref} <linkedin url>{build_flag}`",
          f"- Log who reaches out: `python -m postpilot inbound {batch_ref} --recruiters N --founders N{build_flag}`", "",
          "## Story sources", *[f"- {_safe(u, one_line=True)}" for u in idea.get("source_urls", [])[:5]]]
    return "\n".join(L)


KIND_LABEL = {"demo-app": "LOOK WHAT I BUILT", "open-source-tool": "PUBLIC GITHUB REPO"}

DIFFICULTY_BAR = {"easy": "EASY   [#  ]", "medium": "MEDIUM [## ]", "hard": "HARD   [###]"}


def _hm(minutes: int) -> str:
    return f"~{minutes} min" if minutes < 90 else f"~{minutes / 60:.1f} h".replace(".0 h", " h")


def _mostly_latin(s: str) -> bool:
    return sum(ord(c) < 0x250 for c in s) >= 0.8 * max(1, len(s))


def _clip(s: str, words: int) -> str:
    w = (s or "").split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


def _viral_line(r: dict) -> str:
    """The 3 strongest proofs of what people want, in one line (English titles only)."""
    items = [f'"{_clip(v["title"], 8)}" video ({_short_num(v["views"])} views)'
             for v in r.get("videos", []) if _mostly_latin(v["title"])][:2]
    items += [f'{g["title"].split(":")[0]} repo ({_short_num(g["stars"])} stars)' for g in r.get("repos", [])][:1]
    items += [f'HN thread ({h["points"]} points)' for h in r.get("hn", [])][:1]
    return " · ".join(items[:3])


def _short_num(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def _wrap(label: str, text: str, width: int = 88) -> list[str]:
    """'   Label: text' wrapped, continuation lines indented under the text."""
    first = f"   {label}: "
    lines = textwrap.wrap(text or "", width=width - len(first), break_on_hyphens=False) or [""]
    return [first + lines[0]] + ["     " + ln for ln in textwrap.wrap(" ".join(lines[1:]), width=width - 5,
                                                                      break_on_hyphens=False)]


def _inspired(o: dict) -> str:
    src = o.get("inspired_by") or ""
    return src + (f". Your twist: {o['twist']}" if o.get("twist") else "")


def render_options(batch_id: int, choice: int, idea: dict, res: dict) -> str:
    """The email: the story in 2 lines, then each option in plain words (~7 lines), enough to
    choose without knowing the tool. Full details come with the build kit."""
    opts = res["options"]
    out = [f"PostPilot #{batch_id}.{choice}{res.get('tag', '')}: what to build on "
           f"{idea.get('handle') or idea['topic']}",
           f"Reply with a number (1-{len(opts)}) and I'll send the code, the steps and the post draft. "
           "Or 'skip'.", ""]
    intro = res.get("story_intro") or idea.get("the_news") or ""
    if intro:
        out += ["THE STORY", *[ln for ln in textwrap.wrap(intro, 88)], ""]
    viral = _viral_line(res.get("research") or {})
    if viral:
        out += [*textwrap.wrap("Already viral: " + viral, 88), ""]
    for n, o in enumerate(opts, 1):
        diff = (o.get("difficulty") or "").lower()
        label = KIND_LABEL.get(o["kind"], o["kind"].upper().replace("-", " "))
        pick = "   ★ MY PICK" if n == res["recommended"] else ""
        out += ["-" * 60, f"{n}. {o.get('short_title') or o['title']}   [{label}]{pick}"]
        out += _wrap("What it is", o.get("what_it_is") or o["what_you_build"])
        out += _wrap("What you show", o.get("what_you_show") or o["post_shows"])
        out += _wrap("Why it gets you hired", o.get("why_hired") or o["why_hiring_cares"])
        if o.get("inspired_by"):
            out += _wrap("Inspired by", _inspired(o))
        out += [f"   Effort: {diff} · {_hm(o['minutes']).lstrip('~')} · {_cost_text(o).split(' (')[0]}", ""]
    out += ["-" * 60, f"Why I pick #{res['recommended']}: {res['recommended_reason']}"]
    return "\n".join(out)


def render_options_full(batch_id: int, choice: int, idea: dict, res: dict) -> str:
    """The long version (saved to a file, and its details go into the build kit)."""
    opts = res["options"]
    out = [f"PostPilot #{batch_id}.{choice}{res.get('tag', '')}: what to build on \"{idea['title']}\"", "",
           f"Reply with a number (1-{len(opts)}) and I'll write the code, the steps and the post. "
           "Or 'skip'.", "", "QUICK VIEW"]
    for n, o in enumerate(opts, 1):
        diff = (o.get("difficulty") or "").lower()
        out.append(f"  {n}. {o['title']}  |  {diff or '?'}  |  {_hm(o['minutes'])}  |  "
                   f"{_cost_text(o).split(' (')[0]}")
    out += [f"  My pick: #{res['recommended']}. {res['recommended_reason']}", ""]
    r = res.get("research") or {}
    if any(r.get(k) for k in ("videos", "repos", "hn")):
        out += ["=" * 60, "WHAT'S ALREADY VIRAL (what the options learn from)"]
        out += [f"  YouTube  {v['views']:>10,} views  {v['title'][:80]}" for v in r.get("videos", [])[:4]]
        out += [f"  GitHub   {g['stars']:>10,} stars  {g['title'][:80]}" for g in r.get("repos", [])[:4]]
        out += [f"  HN       {h['points']:>10,} pts    {h['title'][:80]}" for h in r.get("hn", [])[:2]]
        out.append("")
    for n, o in enumerate(opts, 1):
        diff = (o.get("difficulty") or "").lower()
        out += [
            "=" * 60,
            f"{n}. {o['title']}",
            f"   {DIFFICULTY_BAR.get(diff, diff.upper())}  {o['difficulty_why']}",
            f"   TIME: {_hm(o['minutes'])}   |   COST: {_cost_text(o)}   |   TYPE: {KIND_LABEL.get(o['kind'], o['kind'])}",
            "",
            f"   INSPIRED BY: {o.get('inspired_by', '')}",
            f"   YOUR EDGE: {o.get('your_edge', '')}",
            f"   WHAT YOU BUILD: {o['what_you_build']}",
            f"   WHAT THE POST SHOWS: {o['post_shows']}",
            f"   WHY HIRING PEOPLE CARE: {o['why_hiring_cares']}",
            f"   WHO IT IMPRESSES: {o['who_it_impresses']}",
            f"   WHY IT STANDS OUT: {o['stand_out']}",
            "   STEPS:",
            *[f"     {k}. {s}" for k, s in enumerate(o.get("steps_preview", []), 1)],
            *( [f"   MODELS: {', '.join(m.split(':', 1)[1] for m in o['models_to_run'])}"]
               if o.get("models_to_run") else []),
            f"   YOU NEED: {', '.join(o.get('needs', [])) or 'nothing extra'}",
            f"   RISK: {o['risk']}",
            f"   OPENING LINE: {o['hook']}",
            *( ["   Check before you post:", *[f"     ! {c}" for c in o["check_before_posting"]]]
               if o.get("check_before_posting") else []),
            "",
        ]
    out += ["=" * 60, f"Story: {idea['topic']}",
            *[f"  {u}" for u in idea.get("source_urls", [])[:3]]]
    return "\n".join(out)


# ---------- HTML versions (Gmail-safe: inline styles only) ----------

FONT = "font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif"
WRAP = f'<div style="{FONT};max-width:700px;margin:0 auto;color:#1f2328;font-size:15px;line-height:1.55">'
TAG_COLORS = {"LOOK WHAT I BUILT": "#1a7f37", "PUBLIC GITHUB REPO": "#0969da", "NEW MODEL": "#8250df",
              "VIRAL GITHUB REPO": "#0969da", "NEW PRODUCT": "#bf3989", "NEW RESEARCH": "#9a6700"}
URL = re.compile(r"(https?://[^\s<>\"'`*)\]\x00]+)")
CODE = '<code style="background:#f6f8fa;padding:1px 4px;border-radius:4px;font-size:13px">'


def _inline(text: str) -> str:
    """Escape, then make `code` monospace, links clickable and **bold** bold. Code spans are set
    aside first so no tag ever lands inside a link's href."""
    t = html.escape(text, quote=False)
    spans: list[str] = []

    def keep(m):
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"
    t = re.sub(r"`([^`]+)`", keep, t)
    t = URL.sub(lambda m: f'<a href="{m.group(1)}" style="color:#0969da">{_short_url(m.group(1))}</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"{CODE}{spans[int(m.group(1))]}</code>", t)


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
