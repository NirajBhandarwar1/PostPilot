"""The cycle: scout -> fact-gated ideas -> email -> (your reply) -> build -> email the draft.

          sources.collect()                         ~130 live signals
                 │
          brain.find_topics()  ─► _heat() ─► filter_links / dedupe / history ─► chosen[≤3]
                 │                                                             │
   factgate.load_catalogs() ─► shortlist ─► model ids ─► brain.make_ideas(chosen, ids)
                                                                │
                              factgate.check_idea() per idea ◄──┘
                                 fail ─► brain.make_one_idea() ─► check again ─► drop
                                 │
               ≥2 kept ─► normal batch │ 1 kept ─► "short batch" │ 0 kept ─► logged, nothing sent
"""

import json
import logging
import math
import re
import urllib.parse
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import emailhtml, research, brain, db, factgate, llm, mailer, sources
from .config import ROOT, settings
from .vendor import detect as detector
from .vendor import humanize as humanizer

log = logging.getLogger("postpilot")
HOOKS = json.loads((ROOT / "postpilot" / "vendor" / "hooks.json").read_text(encoding="utf-8"))
LEXICON = humanizer.load_lexicon()


def _voice() -> str:
    try:
        text = settings.voice_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    # An untouched template has empty answers; treat it as missing.
    # Answers in (brackets) are the template's hints, not real answers.
    filled = [l for l in text.splitlines() if re.match(r"^- \*\*.+:\*\*\s*[^\s(]", l)]
    return text if filled else ""


HISTORY_DAYS = 14


def _history() -> list[dict]:
    """Stories you actually built or posted in the last 14 days, with their heat at the time.
    Skipped and unpicked batches don't count (eng review O4)."""
    out = []
    cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_DAYS * 86400
    for b in db.recent_batches(50):
        if b["status"] not in ("built", "posted") or not b["chosen"] or not b["built_at"]:
            continue
        if datetime.fromisoformat(b["built_at"]).timestamp() < cutoff:
            continue
        ideas = json.loads(b["ideas_json"]).get("ideas", [])
        if 1 <= b["chosen"] <= len(ideas):
            i = ideas[b["chosen"] - 1]
            out.append({"topic": i.get("topic") or i["title"], "heat": i.get("heat", 0)})
    return out


def _history_names(history: list) -> list[str]:
    return [h["topic"] if isinstance(h, dict) else str(h) for h in history]


def _already_used(topic: dict | str, history: list) -> bool:
    """True if this story was built recently, unless it is now at least twice as hot."""
    name = (topic["name"] if isinstance(topic, dict) else topic).lower()
    heat = topic.get("heat", 0) if isinstance(topic, dict) else 0
    for h in history:
        old_name = (h["topic"] if isinstance(h, dict) else str(h)).lower()
        old_heat = h.get("heat", float("inf")) if isinstance(h, dict) else float("inf")
        if old_name and (name in old_name or old_name in name) and heat < 2 * old_heat:
            return True
    return False


MODEL_SCORE_CAP = 15.0   # the model's own opinion (relevance + buzz) can add at most this
# Stories you can get your hands on beat news-only ones (the user's rule: test it, build with it).
HANDLE_BONUS = {"model": 8.0, "github_repo": 8.0, "product": 5.0, "paper": 4.0}


def _model_notes(cat: factgate.Catalog, keys: list[str]) -> dict:
    """How each offered model is shown to the writer: NEW tag and price."""
    by_key, out = cat.by_key(), {}
    for k in keys:
        m = by_key.get(k)
        tags = []
        if m and m.new:
            tags.append("NEW")
        if m and m.paid:
            tags.append(f"${m.price_in:g}in/${m.price_out:g}out" if m.price_in is not None else "paid")
        out[k] = k + (f"  [{', '.join(tags)}]" if tags else "")
    return out


def _check_handle(t: dict, gh_lookup) -> None:
    """A github_repo handle must be a real repo; add its facts, or fall back to news."""
    if t.get("handle_type") != "github_repo" or gh_lookup is None:
        return
    repo = re.sub(r"^https?://github\.com/", "", (t.get("handle") or "").strip()).strip("/")
    info = gh_lookup(repo) if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else None
    if not info:
        log.info("handle %r is not a real GitHub repo; treating %r as news", repo, t["name"])
        t["handle_type"], t["handle"] = "news", ""
        return
    t["handle"] = info["full_name"]
    t["handle_facts"] = (f"github.com/{info['full_name']}: {info.get('stars', 0):,} stars, "
                         f"{info.get('language') or 'unknown language'}, {info.get('description') or ''}")[:300]


def _heat(topic: dict, signals: list[dict], ids: list[int] | None = None) -> tuple[float, str]:
    """Measured attention only: upvotes, curated front pages, trending ranks, capped coverage.
    `ids` should be the sources that really name the story (link_ids)."""
    ids = topic["signal_ids"] if ids is None else ids
    hits = [signals[i] for i in ids if 0 <= i < len(signals)]
    kinds = {}
    for h in hits:
        kinds.setdefault(h["kind"], []).append(h)
    # Real attention beats article counts: upvotes, curated front pages, trending ranks.
    score = 3 * len(kinds)                                   # seen in many places = viral
    news_pts = 0.0
    for h in hits:
        kind, rank, eng = h["kind"], h["rank"], h.get("engagement", 0) or 0
        if kind.startswith("Hacker News"):
            pts = min(20.0, 4 * math.log2(eng / 100 + 1)) if eng else 3.0   # 959 -> ~14, 2293 -> ~18
        elif kind == "Techmeme":
            pts = 6.0 if rank < 5 else 4.0                   # curated by the industry itself
        elif kind == "Reddit":
            pts = 4.0 if rank < 3 else 2.0
        elif kind == "GitHub trending":
            pts = min(15.0, 3 * math.log2(eng / 1000 + 1)) if eng else 1.0   # 5k -> ~8, 30k+ -> 15
        elif kind.startswith("Hugging Face"):
            pts = 5.0 if rank < 3 else 3.0 if rank < 10 else 1.5
        else:                                                # plain news coverage
            news_pts += 1.0
            continue
        score += pts
    score += min(news_pts, 8)                                # coverage helps, but is capped
    parts = []
    for kind, hs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        best = min(hs, key=lambda h: h["rank"])
        parts.append(f"{kind} x{len(hs)}" if len(hs) > 1 else f"{kind}: {best['detail']}")
    return score, " | ".join(parts)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def _hf_names(signals: list[dict]) -> list[str]:
    """Model repo ids from the Hugging Face trending signals ("owner/name (tag)")."""
    return [s["title"].split(" (")[0] for s in signals if s["kind"].startswith("Hugging Face")]


# ---------- step 1: ideas, fact-gated ----------

def _finish(idea: dict, n: int, t: dict, signals: list[dict], verdict, cat) -> None:
    """Attach the topic, sources, gate results and cost to an idea that passed."""
    idea.update({
        "topic_number": n, "topic": t["name"], "topic_kind": t.get("kind", ""),
        "heat": t["heat"], "heat_where": t["heat_where"],
        "entities": t.get("entities", []),
        "source_urls": list(dict.fromkeys(signals[i]["url"] for i in t["link_ids"])),
        "evidence_titles": [signals[i]["title"] for i in t["link_ids"]],
        "evidence": t.get("evidence", []), "handle_facts": t.get("handle_facts", ""),
        "check_before_posting": verdict.unverified,
        "handle_type": t.get("handle_type", "news"), "handle": t.get("handle", ""),
        "est_cost": factgate.estimate_cost(idea.get("models_to_run") or [], cat),
        "proof": f"experiments/{date.today()}-{_slug(idea['title'])}/",
    })


def generate_batch(signals: list[dict], history: list[str], voice: str, cat: factgate.Catalog,
                   hf_lookup=None, gh_lookup=None) -> dict | None:
    """Find hot topics, write one idea per topic, fact-check each idea, regenerate a failing
    idea once, drop it if it still fails. Returns the batch dict, or None if no idea passed."""
    topics = [t.model_dump() for t in brain.find_topics(signals).topics]
    for t in topics:
        t["link_ids"] = factgate.filter_links(t, signals)   # sources that really name the topic
        t["heat"], t["heat_where"] = _heat(t, signals, t["link_ids"])
        t["evidence"] = [f"[{signals[i]['kind']}] {signals[i]['title']} ({signals[i]['detail']})"
                         for i in t["link_ids"][:6]]
    # Measured heat leads; the model's opinion (builder relevance, buzz) is capped (eng review O3).
    for t in topics:
        _check_handle(t, gh_lookup)
        t["score"] = (t["heat"] + min(MODEL_SCORE_CAP, t.get("builder_relevance", 5) + 0.5 * t.get("buzz", 5))
                      + HANDLE_BONUS.get(t.get("handle_type", "news"), 0.0))
    topics.sort(key=lambda t: -t["score"])
    for t in topics:
        log.info("topic %-55s heat %4.0f buzz %2d links %d  %s", t["name"][:55], t["heat"],
                 t["buzz"], len(t["link_ids"]), t["heat_where"][:60])

    fresh, used_ids = [], set()
    for t in topics:  # hottest first; drop repeats of earlier posts, stories already picked,
        ids = set(t["signal_ids"])            # and stories with no source that names them
        dup = ids and len(ids & used_ids) > len(ids) / 2
        if not t["link_ids"] or _already_used(t, history) or dup:
            continue
        fresh.append(t)
        used_ids |= ids
    chosen = fresh[:3]
    if not chosen:
        log.warning("no fresh hot topic with a matching source this run")
        return None

    focus = [e for t in chosen for e in t.get("entities", [])] + _hf_names(signals)
    model_keys = [m.key for m in factgate.shortlist(cat, focus)]
    notes = _model_notes(cat, model_keys)
    names = _history_names(history)
    raw = brain.make_ideas(chosen, names, voice, HOOKS, model_keys, notes=notes).model_dump()
    by_number = {i["topic_number"]: i for i in raw["ideas"]}
    hf_names = _hf_names(signals)

    stats = {"pitched": len(chosen), "failed_first": 0, "regenerated": 0, "dropped": 0, "unverified": 0}
    kept = []
    for n, t in enumerate(chosen, 1):
        idea = by_number.get(n)
        verdict = (factgate.check_idea(idea, t, signals, cat, hf_names, hf_lookup) if idea
                   else factgate.Verdict(False, ["the writer returned no idea for this topic"]))
        if not verdict.ok:
            stats["failed_first"] += 1
            log.info("idea %d failed the fact gate: %s", n, "; ".join(verdict.reasons))
            try:
                idea = brain.make_one_idea(t, n, names, voice, HOOKS, model_keys,
                                           verdict.reasons, notes=notes).model_dump()
                stats["regenerated"] += 1
                verdict = factgate.check_idea(idea, t, signals, cat, hf_names, hf_lookup)
            except Exception as e:
                verdict = factgate.Verdict(False, [f"regeneration failed: {e}"])
        if not verdict.ok:
            stats["dropped"] += 1
            log.warning("dropped idea for %r: %s", t["name"], "; ".join(verdict.reasons))
            continue
        _finish(idea, n, t, signals, verdict, cat)
        stats["unverified"] += len(verdict.unverified)
        kept.append(idea)

    # A dropped idea is replaced by the next hot story (one try each, at most 2 extra stories).
    for n, t in enumerate(fresh[3:7], len(chosen) + 1):
        if len(kept) >= 3:
            break
        stats["pitched"] += 1
        try:
            idea = brain.make_one_idea(t, n, names, voice, HOOKS, model_keys, [], notes=notes).model_dump()
            verdict = factgate.check_idea(idea, t, signals, cat, hf_names, hf_lookup)
        except Exception as e:
            verdict = factgate.Verdict(False, [f"backfill failed: {e}"])
        if not verdict.ok:
            stats["dropped"] += 1
            log.warning("dropped backfill idea for %r: %s", t["name"], "; ".join(verdict.reasons))
            continue
        _finish(idea, n, t, signals, verdict, cat)
        stats["unverified"] += len(verdict.unverified)
        kept.append(idea)
    if not kept:
        log.warning("all %d ideas failed the fact gate; nothing to send", len(chosen))
        return {"ideas": [], "stats": stats}

    # Number ideas 1..N as shown; "My pick" and replies use this numbering. If the
    # recommended idea was dropped, the hottest remaining idea (first) becomes the pick.
    positions = {i["topic_number"]: p for p, i in enumerate(kept, 1)}
    rec_pos = positions.get(raw.get("recommended"))
    return {
        "ideas": kept,
        "recommended": rec_pos or 1,
        "recommended_reason": raw.get("recommended_reason", "") if rec_pos
                              else "Hottest story that passed the fact check.",
        "also_hot": [t["name"] for t in fresh[3:8] if t["name"] not in {i["topic"] for i in kept}][:3],
        "short": len(kept) < 2,
        "stats": stats,
        "writers": sorted(llm.USAGE),
    }


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
        out += ["-" * 60, *__import__("textwrap").wrap("Also hot (not picked): " + ", ".join(batch["also_hot"]), 88)]
    out += ["-" * 60, f"Why I pick #{batch['recommended']}: {batch['recommended_reason']}",
            "", "Optional: rate the ideas 1-5 to teach me your taste, e.g. '2 rate 4 5 2'."]
    writers = batch.get("writers") or []
    primary = (llm.providers("writer") or [None])[0]
    if writers and primary and not any(w.startswith(primary.name + ":") for w in writers):
        out.append(f"Note: {primary.name} did not answer (outage or daily cap), so a backup model "
                   f"({', '.join(writers)}) wrote this batch. Expect weaker ideas.")
    return "\n".join(out)


def save_run(signals: list[dict], history: list[str], cat: factgate.Catalog) -> Path:
    """Snapshot what this run saw, so `postpilot eval` can replay it later (D10)."""
    runs = settings.data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{datetime.now():%Y-%m-%d-%H%M%S}.json"
    path.write_text(json.dumps({"date": date.today().isoformat(), "signals": signals,
                                "history": history, "catalog": cat.to_json()},
                               ensure_ascii=False), encoding="utf-8")
    return path


def scout(send_email: bool = True) -> int | None:
    llm.reset_usage()                       # this batch's tokens only
    signals = sources.collect()
    ideas_dir = settings.output_dir / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)

    if not llm.available():
        path = ideas_dir / f"{date.today()}-signals.md"
        path.write_text("# Raw trend signals (no model provider set up yet)\n\n" + "\n".join(
            f"- [{c['kind']}] {c['title']} ({c['detail']}) {c['url']}" for c in signals), encoding="utf-8")
        log.warning("No model provider configured: saved %d raw signals to %s", len(signals), path)
        return None

    cat = factgate.load_catalogs()
    log.info("free catalogs: %d models (unavailable: %s, from cache: %s)",
             len(cat.models), cat.unavailable or "none", cat.from_cache or "none")
    history = _history()
    run_path = save_run(signals, history, cat)
    batch = generate_batch(signals, history, _voice(), cat, hf_lookup=factgate.hf_model,
                           gh_lookup=factgate.github_repo)
    db.set_meta("last_scout_at", db.now_iso())
    if batch:
        snap = json.loads(run_path.read_text(encoding="utf-8"))
        snap["stats"] = batch["stats"]
        run_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    if not batch or not batch["ideas"]:
        log.warning("no ideas passed this run; nothing saved or sent")
        return None

    batch_id = db.add_batch(batch, status="draft")
    text = render_ideas(batch_id, batch)
    (ideas_dir / f"{date.today()}-#{batch_id}.md").write_text(text, encoding="utf-8")
    st = batch["stats"]
    log.info("batch #%d: %d pitched, %d failed first check, %d regenerated, %d dropped, %d unverified",
             batch_id, st["pitched"], st["failed_first"], st["regenerated"], st["dropped"], st["unverified"])
    for model, u in llm.USAGE.items():
        log.info("tokens %s: %d calls, %d in, %d out", model, u["calls"], u["in"], u["out"])

    if send_email and settings.has_email:
        mailer.send(mailer.subject_for(batch_id, "What's hot in AI: reply with a number"), text,
                    html=emailhtml.text_email(text))
        db.update_batch(batch_id, status="sent")
    else:
        log.info("email not configured or disabled: ideas saved to %s", ideas_dir)
    return batch_id


# ---------- step 2: build ----------

def build(batch_id: int, choice: int, send_email: bool = True, option: int | None = None) -> Path:
    row = db.get_batch(batch_id)
    if not row:
        raise ValueError(f"No batch #{batch_id}")
    data = json.loads(row["ideas_json"])
    ideas = data["ideas"]
    if not 1 <= choice <= len(ideas):
        raise ValueError(f"Batch #{batch_id} has ideas 1-{len(ideas)}")
    idea = ideas[choice - 1]
    if option:
        opts = (data.get("options") or {}).get(str(choice), {}).get("options", [])
        if not 1 <= option <= len(opts):
            raise ValueError(f"Idea {choice} of batch #{batch_id} has no option {option} "
                             f"(run: python -m postpilot options {batch_id} {choice})")
        idea = _idea_from_option(idea, opts[option - 1],
                                 (data.get("options") or {}).get(str(choice), {}).get("research"))
    log.info("building #%d idea %d%s: %s", batch_id, choice, f" option {option}" if option else "",
             idea["title"])

    llm.reset_usage()                       # so the plan can say which model wrote it
    found = idea.get("research") or {}
    docs, docs_from = found.get("docs", ""), found.get("docs_from", "")
    if not docs:
        docs, docs_from = research.docs_for(idea)
    brief = brain.build_brief(idea, research.as_text(found), docs, docs_from)

    # The post: humanize and score; one rewrite if it still reads machine-written.
    kit, clean, report, before, after = _write_post(idea, _plan_summary(brief))

    # D12: fact-check the post and the plan with the same scans as the pitch.
    titles = idea.get("evidence") or idea.get("evidence_titles", [])
    pseudo_signals = [{"title": t} for t in titles]
    topic = {"signal_ids": list(range(len(titles))), "entities": idea.get("entities", [])}
    try:
        cat = factgate.load_catalogs()
    except Exception:
        cat = factgate.Catalog()
    runs = idea.get("models_to_run", [])
    post_checks = factgate.scan_text([clean, *kit.alt_hooks], topic, pseudo_signals, cat, runs)
    plan_text = [brief.goal, brief.hot_thing_usage, *[t.details for t in brief.tasks], *brief.stack]
    plan_checks = factgate.scan_text(plan_text, topic, pseudo_signals, cat, runs)
    # R1: the setup commands Cursor will run are scanned like code (unknown installs, shell tricks).
    # Hosts the plan may legitimately use: code hosting, package index, the story's own sources.
    known = {"github.com", "raw.githubusercontent.com", "pypi.org", "files.pythonhosted.org"}
    known |= {re.sub(r"^www\.", "", urllib.parse.urlparse(u).netloc) for u in idea.get("source_urls", [])}
    plan_checks += factgate.scan_code([("setup", "\n".join(brief.setup_commands))], extra_hosts=known)
    checks = [*idea.get("check_before_posting", []), *post_checks, *kit.fact_checks]

    out = settings.output_dir / "builds" / f"{date.today()}-#{batch_id}-{_slug(idea['title'])}"
    out.mkdir(parents=True, exist_ok=True)
    doc = render_brief(idea, brief, kit, clean, (before[1], after[1], after[2]), report, checks, plan_checks)
    for w in brief_warnings(idea, brief):
        log.warning("BUILD.md check: %s", w)
    (out / "BUILD.md").write_text(doc, encoding="utf-8")
    db.update_batch(batch_id, status="built", chosen=choice, built_at=db.now_iso(), build_dir=str(out))

    if send_email and settings.has_email:
        intro = (f"{kit.summary}\nYour build plan is attached (BUILD.md) and is also below.\n"
                 "1. Do the 'Before you start' list.\n"
                 "2. Make an empty folder, open it in Cursor, and put BUILD.md in it.\n"
                 f"3. In Cursor's Agent chat, paste: {CURSOR_PROMPT}\n"
                 "4. When it works, follow Part B to record the demo and post.\n")
        body = intro + "\n" + "=" * 60 + "\n\n" + doc
        mailer.send(mailer.subject_for(batch_id, f"Build plan ready: {idea['title'][:60]}", idea=choice),
                    body, html=emailhtml.markdown_email(intro, doc), attachments=[("BUILD.md", doc)])
    log.info("build plan saved to %s", out / "BUILD.md")
    return out


CURSOR_PROMPT = ("Read BUILD.md. Build Part A one step at a time. Do the 🤖 steps yourself; when you reach "
                 "a 🧑 step, stop and tell me exactly what to do, then wait. After each step run its "
                 "\"Done when\" check and fix any error before moving on. Follow the Rules strictly. "
                 "Stop at the end of Part A.")


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


def _plan_summary(brief) -> str:
    """What the post writer must stay consistent with."""
    return "\n".join([f"Repo: {_slug(brief.project_name)}", f"Goal: {brief.goal}",
                      f"How the hot thing is used: {brief.hot_thing_usage}", f"Sample data: {brief.demo_data}",
                      *[f"- {t.title}: {t.details[:400]}" for t in brief.tasks]])


POST_TARGET = 75          # aim above the scorer's 70 pass line
POST_TRIES = 3           # first draft + up to 2 rewrites


def _score(text: str):
    """Score as the finished post will read: {{placeholders}} become real results (numbers) once
    the person fills them in, so they count as a number, not as nothing."""
    return detector.run(re.sub(r"\{\{[^}]*\}\}", "12", text), LEXICON)


def _facts(idea: dict) -> str:
    r = idea.get("research") or {}
    out = [f"- YouTube video \"{v['title']}\": {v['views']:,} views" for v in r.get("videos", [])[:3]]
    out += [f"- GitHub repo {g['title'].split(':')[0]}: {g['stars']:,} stars" for g in r.get("repos", [])[:3]]
    out += [f"- Hacker News thread \"{h['title']}\": {h['points']} points, {h['comments']} comments"
            for h in r.get("hn", [])[:2]]
    if idea.get("handle_facts"):
        out.append(f"- {idea['handle_facts']}")
    return "\n".join(out)


def _write_post(idea: dict, plan: str = ""):
    """Post draft -> humanizer -> score. While it is under target, rewrite with the exact failing
    checks as feedback (up to POST_TRIES drafts), and keep the best one."""
    best, feedback, facts = None, "", _facts(idea)
    for attempt in range(POST_TRIES):
        kit = brain.build_post(idea, _voice(), HOOKS, feedback=feedback, plan=plan, facts=facts)
        clean, report = humanizer.humanize(kit.post_draft, LEXICON)
        before, after = _score(kit.post_draft), _score(clean)
        if best is None or after[1] > best[4][1]:
            best = (kit, clean, report, before, after)
        if after[1] >= POST_TARGET and after[2] == "PASS":
            break
        weak = [(n, d) for n, (sc, d) in sorted(after[0].items(), key=lambda kv: kv[1][0]) if sc < 75][:3]
        feedback = (f"REWRITE: the previous draft scored {after[1]:.0f}/100 on the human-writing check "
                    f"(target {POST_TARGET}). Fix exactly these: "
                    + "; ".join(f"{n.lower()}: {d}" for n, d in weak)
                    + ". Keep the facts and the structure; change the wording.")
        log.info("post draft %d scored %.0f (%s)%s", attempt + 1, after[1], after[2],
                 "; rewriting" if attempt + 1 < POST_TRIES else "; keeping the best")
    return best


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


def _requirements(brief) -> list[str]:
    req = {r.strip() for r in brief.requirements if r.strip()} | {"pytest"}
    if brief.uses_models:
        req |= {"openai", "python-dotenv"}
    return sorted(req, key=str.lower)


def render_brief(idea, brief, kit, post: str, score, report, checks, plan_checks) -> str:
    """BUILD.md: Part A for Cursor (fixed first/last steps around the model's tasks), Part B for you."""
    runs = idea.get("models_to_run", []) if brief.uses_models else []
    env = sorted({PROVIDER_API[k.split(":")[0]][1] for k in runs if k.split(":")[0] in PROVIDER_API})
    if any(k.startswith("cloudflare:") for k in runs):
        env.insert(0, "CLOUDFLARE_ACCOUNT_ID")
    opt = idea.get("option") or {}
    slug = _slug(brief.project_name)
    req = _requirements(brief)
    commands = [c for c in brief.setup_commands if not _as_command(c).startswith("#")]
    prose = [c for c in brief.setup_commands if _as_command(c).startswith("#")]
    total = sum(t.minutes for t in brief.tasks) + sum(FIXED_MINUTES.values())
    warnings = brief_warnings(idea, brief)

    L = [f"# BUILD: {idea['title']}", "",
         f"Story: {idea.get('topic', '')}  |  Type: {opt.get('kind', idea.get('format', ''))}  |  "
         f"Difficulty: {opt.get('difficulty', '?')}  |  Realistic time: ~{_hm(total).lstrip('~')}", "",
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
    L += ["---", "", "# PART A: Build instructions (for Cursor)", "",
          "## Goal", brief.goal, "", f"Repo name: `{slug}`", "",
          "## Before you start (🧑 you, once)",
          *[f"- [ ] {x}" for x in [*brief.before_you_start, *prose]],
          *([f"- [ ] Get keys for `.env`: " + "; ".join(f"{v[1]} from {v[2]}" for v in PROVIDER_API.values()
                                                         if v[1] in env)] if env else []),
          "- [ ] Python: `py -3.12 --version` prints 3.12.x. Git: `git --version` works.", ""]
    if brief.hot_thing_usage:
        L += [f"## How {idea.get('handle') or 'the hot thing'} is used in this build", brief.hot_thing_usage, ""]
    L += ["## Done means", *[f"- [ ] {c}" for c in brief.done_criteria], "",
          "## Stack", *[f"- {x}" for x in brief.stack], "",
          "## Files", *[f"- `{f.path}`: {f.purpose}" for f in brief.file_tree], ""]
    if brief.visual_spec:
        L += ["## Look and feel (the demo video sells this)", brief.visual_spec, ""]
    if runs:
        L += ["## AI models the code may call (only these)",
              "All are OpenAI-compatible: use the `openai` package with `base_url` and the key below.", "",
              "| model id | base_url | key in .env |", "| --- | --- | --- |"]
        for k in runs:
            prov, mid = k.split(":", 1)
            base, key, _ = PROVIDER_API.get(prov, ("?", "?", "?"))
            L.append(f"| `{mid}` | `{base}` | `{key}` |")
        L.append("")
    L += ["## Sample data", brief.demo_data, "", "## Steps", ""]

    # Step 1 is always the same, so it is always right.
    env_lines = [f"{e}=" for e in env] + (["MAX_CALLS=60"] if runs else ["# no keys needed"])
    L += ["### Step 1 🤖: Create the project  (~10 min)",
          "Run these in the project folder (PowerShell):", "```powershell", "git init",
          "py -3.12 -m venv .venv",
          f"Set-Content requirements.txt \"{chr(96)}n\".Join(@({', '.join(repr(r) for r in req)}))".replace("'", '"'),
          ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt", *commands, "```",
          "Create `.gitignore` with: `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.db`, `node_modules/`.",
          "Create `.env.example` with:", "```", *env_lines, "```",
          "", "**Done when:** `git check-ignore .env` prints `.env`, and "
          "`.\\.venv\\Scripts\\python.exe -m pytest --version` prints a version.", "",
          f"All later steps run Python as `{brain.VENV_PY}` and tests as `{brain.TEST_CMD}`.", ""]
    for n, t in enumerate(brief.tasks, 2):
        title = re.sub(r"^(?:step\s*\d+\s*[:.)-]\s*)?(?:\d+\s*[.):-]\s*)?", "", t.title, flags=re.I)
        mark = "🧑" if (t.who or "").lower().startswith("you") else "🤖"
        L += [f"### Step {n} {mark}: {title}  (~{t.minutes} min)", t.details, "", f"**Done when:** {t.done_when}", ""]
    n = len(brief.tasks) + 2
    L += [f"### Step {n} 🧑: Record the demo and add the GIF  (~15 min)",
          "Follow the shot list in Part B. Record with Win+Alt+R (Xbox Game Bar) or ScreenToGif (free). "
          "Save a 10-15 second GIF as `docs/demo.gif`; Cursor then adds it at the top of README.md.", "",
          "**Done when:** `docs/demo.gif` exists and shows in the README preview.", "",
          f"### Step {n + 1} 🧑: Publish the repo on GitHub  (~10 min)",
          "Check nothing secret is included, then publish (you run these, not Cursor):", "```powershell",
          "git ls-files .env            # must print nothing", "git add .",
          f"git commit -m \"{slug}: first version\"",
          f"gh repo create {slug} --public --source . --push   # or create it on github.com and push", "```",
          "Add a short repo description and 3-5 topics on GitHub (e.g. the hot thing's name).", "",
          "**Done when:** the repo page shows the README with the GIF.", ""]
    L += [f"## Tests (run with `{brain.TEST_CMD}`; no network, no keys)", *[f"- {x}" for x in brief.tests], "",
          "## Rules (must follow)",
          "- Never print, log or commit secrets. `.env` stays in `.gitignore`.",
          *(["- Read keys only from `.env` (python-dotenv). Call only the models listed above; stop after "
             "`MAX_CALLS` calls and cache responses to disk."] if runs else
            ["- The app itself calls no AI model and needs no keys."]),
          "- Only open-source (permissive license) or self-written targets and data. No scraping against "
          "a site's terms, no copies of paid software, no personal data.",
          "- No deleting files outside this folder; do not run downloaded binaries the person has not approved.",
          "- Keep it small and working: a smaller demo that runs beats a big one that doesn't.",
          "", "## README (public repo)", *[f"- {x}" for x in brief.readme_outline], "",
          "## Known pitfalls", *[f"- {x}" for x in brief.pitfalls]]
    if plan_checks:
        L += ["", "## Check before building", *[f"- {c}" for c in plan_checks]]
    flags = report.get("structures", [])
    L += ["", "**Cursor: stop here. Part B is for the human.**", "", "---", "",
          "# PART B: Demo and LinkedIn post (for you)", "",
          "## 1. Record the demo (30-60 seconds)", *[f"{k}. {x}" for k, x in enumerate(brief.demo_script, 1)], "",
          "## 2. The post (replace every {{...}} with your real results)", "", "```", post.strip(), "```", "",
          f"Human score: {score[1]:.0f}/100 ({score[2]})",
          *(["Lines to rewrite by hand:", *[f"- {s['name']} (x{s['count']}): {s['fix']}" for s in flags]]
            if flags else []), "",
          "## 3. Fill these in", *[f"- {x}" for x in kit.placeholders], "",
          "## 4. Other opening lines", *[f"- {x}" for x in kit.alt_hooks], "",
          "## 5. Image / video", kit.visual_spec, "",
          "## 6. First comment (post it right after publishing)", kit.first_comment or "{{your repo link}}", "",
          "## 7. Check before you post", *([f"- {c}" for c in checks] or ["- nothing flagged"]), "",
          "## 8. After you post",
          "- Record it: `python -m postpilot posted <batch> <linkedin url>`",
          "- Log who reaches out: `python -m postpilot inbound <batch> --recruiters N --founders N`", "",
          "## Story sources", *[f"- {u}" for u in idea.get("source_urls", [])[:5]]]
    return "\n".join(L)


# ---------- step 2b: build options for the picked idea ----------

def _idea_from_option(idea: dict, o: dict, research: dict | None = None) -> dict:
    """The picked option replaces the rough plan; topic, sources and checks stay."""
    return {**idea,
            "title": o["title"], "format": o["kind"], "role_angle": o["why_hiring_cares"],
            "build_plan": o["what_you_build"] + " Steps: " + " ".join(
                f"({n}) {s}" for n, s in enumerate(o.get("steps_preview", []), 1)),
            "build_minutes": o["minutes"], "hook_options": [o["hook"]], "visual": o["post_shows"],
            "needs": o.get("needs", []), "models_to_run": o.get("models_to_run", []),
            "proof": f"experiments/{date.today()}-{_slug(o['title'])}/",
            "option": o, "research": research}


def _check_option(o: dict, idea: dict, cat: factgate.Catalog) -> factgate.Verdict:
    """Same fact gate as the ideas, against the picked story's own sources."""
    ev = idea.get("evidence") or idea.get("evidence_titles", [])
    sigs = [{"title": e, "kind": "", "detail": ""} for e in ev]
    topic = {"signal_ids": list(range(len(sigs))), "entities": idea.get("entities", [])}
    # Only a face-off must list models; other builds may use the tool's own model.
    as_idea = {"title": o["title"], "format": "face-off" if o["kind"] == "face-off" else "build",
               "build_plan": o["what_you_build"] + " " + " ".join(o.get("steps_preview", [])),
               "why_hot": "", "hook_options": [o["hook"]], "needs": o.get("needs", []),
               "models_to_run": o.get("models_to_run", [])}
    return factgate.check_idea(as_idea, topic, sigs, cat)


def propose(batch_id: int, choice: int, send_email: bool = True) -> dict:
    """The person picked idea `choice`: offer 3-5 concrete builds on it, fact-checked and priced."""
    row = db.get_batch(batch_id)
    if not row:
        raise ValueError(f"No batch #{batch_id}")
    data = json.loads(row["ideas_json"])
    if not 1 <= choice <= len(data["ideas"]):
        raise ValueError(f"Batch #{batch_id} has ideas 1-{len(data['ideas'])}")
    idea = data["ideas"][choice - 1]
    log.info("options for #%d idea %d: %s", batch_id, choice, idea["title"])
    llm.reset_usage()
    cat = factgate.load_catalogs()
    keys = [m.key for m in factgate.shortlist(cat, idea.get("entities", []) + [idea.get("handle", "")])]
    notes = _model_notes(cat, keys)

    found = research.research(idea)
    digest = research.as_text(found)
    if found.get("docs"):
        digest += (f"\n\nOfficial docs ({found['docs_from']}): what it is for and how it is used:\n"
                   + found["docs"][:2500])
    known = [t.lower() for t in research.titles(found)]

    kept, raw, dropped = [], {}, []
    extra = ""
    for attempt in range(2):     # one retry if too few options pass, or no "look what I built" demo
        raw = brain.make_options(idea, _voice(), keys, notes, research_text=digest, extra=extra).model_dump()
        kept, dropped = [], []
        for pos, o in enumerate(raw["options"], 1):
            v = _check_option(o, idea, cat)
            if not v.ok:
                dropped.append(f"{o['title']}: {'; '.join(v.reasons)}")
                continue
            src = (o.get("inspired_by") or "").lower()
            if known and not any(_overlap(src, t) for t in known):
                v.unverified.append(f"\"Inspired by\" source not found in the research: {o.get('inspired_by')}")
            o.update({"check_before_posting": v.unverified, "orig_pos": pos,
                      "est_cost": factgate.estimate_cost(o.get("models_to_run") or [], cat)})
            kept.append(o)
        missing = [k for k in REQUIRED_KINDS if not any(o["kind"] == k for o in kept)]
        if len(kept) >= 2 and not missing:
            break
        log.warning("%d option(s) passed, missing kinds: %s; retrying once", len(kept), missing or "none")
        extra = ("IMPORTANT: the previous answer was missing a passing option of kind: " + ", ".join(missing)
                 + ". demo-app = a pure 'look what I built' app, no eval framing; open-source-tool = a public "
                 "GitHub repo others will star or rerun."
                 + (" Dropped options: " + " | ".join(dropped) if dropped else ""))
    for d in dropped:
        log.info("dropped option %s", d)
    if not kept:
        raise RuntimeError("no build option passed the fact check: " + " | ".join(dropped))
    rec = next((n for n, o in enumerate(kept, 1) if o["orig_pos"] == raw.get("recommended")), None)
    shown = {o["orig_pos"]: n for n, o in enumerate(kept, 1)}
    reason = re.sub(r"\b(Option|option|#)\s?(\d)\b",            # writer's numbers -> shown numbers
                    lambda m: f"{m.group(1)}{' ' if m.group(1) != '#' else ''}{shown.get(int(m.group(2)), '(dropped)')}",
                    raw.get("recommended_reason", ""))
    result = {"options": kept, "recommended": rec or 1, "research": found,
              "story_intro": raw.get("story_intro", ""),
              "recommended_reason": reason if rec else "Best remaining option after the fact check.",
              "writers": sorted(llm.USAGE)}
    data.setdefault("options", {})[str(choice)] = result
    db.update_batch(batch_id, ideas_json=json.dumps(data), status="options_sent")

    text = render_options(batch_id, choice, idea, result)
    ideas_dir = settings.output_dir / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    (ideas_dir / f"{date.today()}-#{batch_id}.{choice}-options.md").write_text(text, encoding="utf-8")
    (ideas_dir / f"{date.today()}-#{batch_id}.{choice}-options-full.md").write_text(
        render_options_full(batch_id, choice, idea, result), encoding="utf-8")
    if send_email and settings.has_email:
        mailer.send(mailer.subject_for(batch_id, f"Build options: {idea['title'][:60]}", idea=choice), text,
                    html=emailhtml.text_email(text))
    else:
        log.info("options saved to %s", ideas_dir)
    return result


def _overlap(a: str, b: str) -> bool:
    """Does text a quote source b? (3+ shared distinctive words)"""
    words = lambda s: {w for w in re.findall(r"[a-z0-9]{3,}", s)} - {"the", "and", "for", "with", "views", "stars"}
    return len(words(a) & words(b)) >= 3


REQUIRED_KINDS = ("demo-app", "open-source-tool")   # every options email has both
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
    import textwrap
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
    out = [f"PostPilot #{batch_id}.{choice}: what to build on {idea.get('handle') or idea['topic']}",
           f"Reply with a number (1-{len(opts)}) and I'll send the code, the steps and the post draft. "
           "Or 'skip'.", ""]
    intro = res.get("story_intro") or idea.get("the_news") or ""
    if intro:
        out += ["THE STORY", *[ln for ln in __import__("textwrap").wrap(intro, 88)], ""]
    viral = _viral_line(res.get("research") or {})
    if viral:
        out += [*__import__("textwrap").wrap("Already viral: " + viral, 88), ""]
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
    out = [f"PostPilot #{batch_id}.{choice}: what to build on \"{idea['title']}\"", "",
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


# ---------- the always-on loop ----------

RETRY_AFTER_FAILURE_S = 3600


def scout_due() -> bool:
    retry = db.get_meta("retry_scout_at")
    if retry:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(retry)
    last = db.get_meta("last_scout_at")
    if not last:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    return age.total_seconds() >= settings.cycle_days * 86400


def check_replies() -> None:
    if not settings.has_email:
        return
    for batch_id, idea_n, choice in mailer.fetch_replies():
        handle_reply(batch_id, idea_n, choice)


def handle_reply(batch_id: int, idea_n: int | None, choice: str) -> None:
    """Reply to the ideas email ("3") -> build options; "3 go" -> build my top option now;
    reply to an options email ("#8.3", "2") -> build that option."""
    row = db.get_batch(batch_id)
    if not row or row["status"] == "skipped":
        return
    if choice == "skip":
        if idea_n is None:
            db.update_batch(batch_id, status="skipped")
            log.info("batch #%d skipped by reply", batch_id)
        return
    num, go = int(choice.split()[0]), choice.endswith(" go")
    if idea_n is None and row["status"] == "built" and not go:
        return                                        # already built; ignore a stray re-reply
    try:
        if idea_n is not None:
            build(batch_id, idea_n, option=num)
        elif go:
            res = propose(batch_id, num, send_email=False)
            build(batch_id, num, option=res["recommended"])
        else:
            propose(batch_id, num)
    except Exception as e:
        log.exception("reply handling failed for #%d", batch_id)
        if settings.has_email:
            mailer.send(mailer.subject_for(batch_id, "Something failed", idea=idea_n),
                        f"Error: {e}\nReply again to retry.")


def run_forever() -> None:
    if not llm.available():
        raise SystemExit("Set up a model provider in .env (e.g. GROQ_API_KEY) before starting the loop.")
    log.info("PostPilot running: scout every %g days, check email every %g min",
             settings.cycle_days, settings.check_minutes)
    while True:
        try:
            if scout_due():
                scout()
                db.set_meta("retry_scout_at", "")
        except Exception:
            retry_at = datetime.fromtimestamp(time.time() + RETRY_AFTER_FAILURE_S, timezone.utc)
            log.exception("scout failed; retrying at %s", retry_at.isoformat(timespec="minutes"))
            db.set_meta("retry_scout_at", retry_at.isoformat(timespec="seconds"))
        try:
            check_replies()
        except Exception:
            log.exception("reply check failed")
        time.sleep(settings.check_minutes * 60)


# ---------- replay eval (D10; folded in from evaluate.py per eng review D2) ----------

def replay(n: int = 3) -> dict:
    """Re-run topic finding + idea writing + the fact gate on the last N saved runs, using each
    run's saved catalog so numbers stay comparable. Calls the real writer models."""
    runs = sorted((settings.data_dir / "runs").glob("*.json"))[-n:]
    if not runs:
        print("No saved runs yet. Run `python -m postpilot scout --no-email` first.")
        return {}
    llm.reset_usage()
    keys = ("pitched", "failed_first", "regenerated", "dropped", "unverified")
    total = {"runs": 0, **{k: 0 for k in keys}}
    print(f"{'run':24} {'pitched':>7} {'failed':>6} {'regen':>5} {'dropped':>7} {'unverif':>7}")
    for path in runs:
        snap = json.loads(path.read_text(encoding="utf-8"))
        cat = factgate.Catalog.from_json(snap["catalog"])
        try:
            batch = generate_batch(snap["signals"], snap.get("history", []), _voice(), cat)
        except Exception as e:
            print(f"{path.stem:24} ERROR {e}")
            continue
        st = (batch or {}).get("stats") or {k: 0 for k in keys}
        print(f"{path.stem:24} {st['pitched']:>7} {st['failed_first']:>6} {st['regenerated']:>5} "
              f"{st['dropped']:>7} {st['unverified']:>7}")
        total["runs"] += 1
        for k in keys:
            total[k] += st.get(k, 0)
    if total["pitched"]:
        total["failure_rate"] = 100 * total["failed_first"] / total["pitched"]
        total["unverified_per_idea"] = total["unverified"] / total["pitched"]
        print(f"\nPre-gate failure rate: {total['failure_rate']:.0f}% ({total['failed_first']}/"
              f"{total['pitched']} ideas failed the fact check before it filtered them). Target: under 20%.")
        print(f"Unverified claims per idea: {total['unverified_per_idea']:.1f}. "
              f"Dropped after retry: {total['dropped']}.")
    for model, u in llm.USAGE.items():
        print(f"tokens {model}: {u['calls']} calls, {u['in']} in, {u['out']} out")
    return total


# ---------- outcomes: what your posts actually did (eng review O2) ----------

def report() -> dict:
    """Rank formats and topic kinds by inbound (recruiters + founders + other) per post."""
    rows = db.all_outcomes()
    posted = [r for r in rows if r["posted_url"]]
    if not posted:
        print("No posts logged yet. After posting: python -m postpilot posted <batch> <linkedin-url>")
        return {}
    by = {"format": {}, "topic kind": {}}
    total_in = 0
    print(f"{'batch':>5}  {'inbound':>7}  {'recr':>4} {'fndr':>4} {'othr':>4}  format / topic")
    for r in posted:
        batch = db.get_batch(r["batch_id"])
        ideas = json.loads(batch["ideas_json"]).get("ideas", []) if batch else []
        idea = ideas[batch["chosen"] - 1] if batch and batch["chosen"] and batch["chosen"] <= len(ideas) else {}
        inbound = (r["recruiters"] or 0) + (r["founders"] or 0) + (r["other"] or 0)
        total_in += inbound
        fmt, kind = idea.get("format", "?"), idea.get("topic_kind", "?") or "?"
        for dim, key in (("format", fmt), ("topic kind", kind)):
            agg = by[dim].setdefault(key, {"posts": 0, "inbound": 0})
            agg["posts"] += 1
            agg["inbound"] += inbound
        print(f"#{r['batch_id']:>4}  {inbound:>7}  {r['recruiters'] or 0:>4} {r['founders'] or 0:>4} "
              f"{r['other'] or 0:>4}  {fmt} / {idea.get('topic', '?')[:50]}")
    print(f"\nTotal: {len(posted)} posts, {total_in} inbound ({total_in / len(posted):.1f} per post)")
    for dim, groups in by.items():
        print(f"\nBy {dim} (inbound per post):")
        for key, agg in sorted(groups.items(), key=lambda kv: -kv[1]["inbound"] / kv[1]["posts"]):
            print(f"  {key:22} {agg['inbound'] / agg['posts']:5.1f}  ({agg['posts']} posts)")
    baseline = json.loads(db.get_meta("baseline", "{}") or "{}")
    if baseline:
        print(f"\nBaseline before posting: {baseline}")
    ratings = [json.loads(v) for k, v in _meta_items("ratings:")]
    if ratings:
        flat = [x for r in ratings for x in r]
        print(f"Your idea ratings: {len(ratings)} batches rated, average {sum(flat) / len(flat):.1f}/5")
    return {"posts": len(posted), "inbound": total_in, "by": by}


def _meta_items(prefix: str) -> list[tuple[str, str]]:
    with db.connect() as c:
        return [(r["key"], r["value"]) for r in
                c.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()]
