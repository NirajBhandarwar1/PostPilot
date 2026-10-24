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
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import brain, db, factgate, llm, mailer, sources
from .render import (CURSOR_PROMPT, slug as _slug, brief_warnings, markdown_email, render_brief,
                     render_ideas, render_options, render_options_full, text_email)
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


def _builds(b) -> list[dict]:
    """Every build recorded for a batch row. Old batches (before the builds list) count their
    single chosen idea if the batch was built or posted."""
    data = json.loads(b["ideas_json"])
    if data.get("builds"):
        return data["builds"]
    if b["status"] in ("built", "posted") and b["chosen"] and b["built_at"]:
        return [{"idea": b["chosen"], "option": None, "built_at": b["built_at"], "build_dir": b["build_dir"]}]
    return []


def _history() -> list[dict]:
    """Stories you actually built or posted in the last 14 days, with their heat at the time.
    Every build counts, even several from one batch (eng review O4, D5)."""
    out = []
    cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_DAYS * 86400
    for b in db.recent_batches(50):
        ideas = json.loads(b["ideas_json"]).get("ideas", [])
        for rec in _builds(b):
            if not rec.get("built_at") or datetime.fromisoformat(rec["built_at"]).timestamp() < cutoff:
                continue
            if 1 <= rec["idea"] <= len(ideas):
                i = ideas[rec["idea"] - 1]
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
    log.info("model catalogs: %d models (unavailable: %s, from cache: %s)",
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
                    html=text_email(text))
        db.update_batch(batch_id, status="sent")
    else:
        log.info("email not configured or disabled: ideas saved to %s", ideas_dir)
    return batch_id


# ---------- step 2: build ----------

def _email_build(batch_id: int, choice: int, title: str, summary: str, doc: str) -> None:
    if not settings.has_email:
        return
    intro = (f"{summary}\nYour build plan is attached (BUILD.md) and is also below.\n"
             "1. Do the 'Before you start' list.\n"
             "2. Make an empty folder, open it in Cursor, and put BUILD.md in it.\n"
             f"3. In Cursor's Agent chat, paste: {CURSOR_PROMPT}\n"
             "4. When it works, follow Part B to record the demo and post.\n")
    body = intro + "\n" + "=" * 60 + "\n\n" + doc
    # kind="plan": a reply to this email ("step 4 failed") is never read as a pick.
    mailer.send(mailer.subject_for(batch_id, f"Build plan ready: {title[:60]}", idea=choice, kind="plan"),
                body, html=markdown_email(intro, doc), attachments=[("BUILD.md", doc)])


def _saved_build(data: dict, choice: int, option: int | None, tag: str = "") -> dict | None:
    """The recorded build of this idea + option of this options list (tag) whose BUILD.md still
    exists, if any (D18). After '3 new', option 2 of list 'b' is a different build than option 2
    of the first list."""
    for rec in reversed(data.get("builds", [])):
        d = rec.get("build_dir")
        if rec["idea"] == choice and rec.get("option") == option and \
                (option is None or rec.get("tag", "") == tag) and d and (Path(d) / "BUILD.md").exists():
            return rec
    return None


def _build_dir(batch_id: int, choice: int, option: int | None, tag: str, title: str) -> Path:
    """A folder per build that never overwrites another build's BUILD.md."""
    base = f"{date.today()}-#{batch_id}.{choice}{tag}" + (f"-o{option}" if option else "") + f"-{_slug(title)}"
    (settings.output_dir / "builds").mkdir(parents=True, exist_ok=True)
    out, k = settings.output_dir / "builds" / base, 2
    while True:
        try:
            out.mkdir()                      # atomic: two builds at once never share a folder
            return out
        except FileExistsError:
            out, k = settings.output_dir / "builds" / f"{base}-{k}", k + 1


def build(batch_id: int, choice: int, send_email: bool = True, option: int | None = None,
          reuse: bool = True) -> Path:
    """Write BUILD.md for idea `choice` (option `option` of its options email). With reuse (the
    email path), a repeat of an already-built idea+option resends the saved plan instead of
    paying for a new one (D18); the CLI passes reuse=False to force a fresh plan."""
    row = db.get_batch(batch_id)
    if not row:
        raise ValueError(f"No batch #{batch_id}")
    data = json.loads(row["ideas_json"])
    ideas = data["ideas"]
    if not 1 <= choice <= len(ideas):
        raise ValueError(f"Batch #{batch_id} has ideas 1-{len(ideas)}")
    idea = ideas[choice - 1]
    listing = (data.get("options") or {}).get(str(choice), {})
    tag = listing.get("tag", "") if option else ""
    kind = idea.get("format", "")
    if option:
        opts = listing.get("options", [])
        if not 1 <= option <= len(opts):
            raise ValueError(f"Idea {choice} of batch #{batch_id} has no option {option} "
                             f"(run: python -m postpilot options {batch_id} {choice})")
        kind = opts[option - 1].get("kind", "")
        idea = _idea_from_option(idea, opts[option - 1], listing.get("research"))
    saved = _saved_build(data, choice, option, tag) if reuse else None
    if saved:
        out = Path(saved["build_dir"])
        log.info("idea %d option %s of #%d was already built; resending %s", choice, option, batch_id, out)
        if send_email:
            _email_build(batch_id, choice, saved.get("title") or idea["title"], saved.get("summary", ""),
                         (out / "BUILD.md").read_text(encoding="utf-8"))
        return out
    log.info("building #%d idea %d%s: %s", batch_id, choice, f" option {option}" if option else "",
             idea["title"])

    # The number this build will get, for the "After you post" commands in Part B.
    predicted = len(data.get("builds", [])) + 1
    plan = _make_plan(idea, batch_id=batch_id, build_no=predicted)
    kit, doc = plan["kit"], plan["doc"]
    out = _build_dir(batch_id, choice, option, tag, idea["title"])
    for w in plan["warnings"]:
        log.warning("BUILD.md check: %s", w)
    (out / "BUILD.md").write_text(doc, encoding="utf-8")
    now = db.now_iso()
    rec = {"idea": choice, "option": option, "tag": tag, "kind": kind, "title": idea["title"],
           "built_at": now, "build_dir": str(out), "summary": kit.summary}
    # D5 + D26: append this build to the latest saved data in one write transaction.
    saved = db.edit_batch(batch_id, lambda d: d.setdefault("builds", []).append(rec), status="built",
                          chosen=choice, built_at=now, build_dir=str(out))
    actual = len(saved["builds"])
    if actual != predicted:                  # another build landed meanwhile: fix the post commands
        doc = doc.replace(f" --build {predicted}`", f" --build {actual}`")
        (out / "BUILD.md").write_text(doc, encoding="utf-8")

    if send_email:
        _email_build(batch_id, choice, idea["title"], kit.summary, doc)
    log.info("build plan saved to %s", out / "BUILD.md")
    return out


def _make_plan(idea: dict, batch_id: int | None = None, build_no: int | None = None) -> dict:
    """Write the BUILD.md plan and post for one idea (or picked option). No saving, no email:
    build() and the eval both use this, so the eval measures exactly what you would get."""
    llm.reset_usage()                       # so the plan can say which model wrote it
    found = idea.get("research") or {}
    docs, docs_from = found.get("docs", ""), found.get("docs_from", "")
    if not docs:
        docs, docs_from = sources.docs_for(idea)
    brief = brain.build_brief(idea, sources.research_digest(found), docs, docs_from)

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
    # R1: setup commands are scanned like code inside render_brief, next to each command.
    checks = [*idea.get("check_before_posting", []), *post_checks, *kit.fact_checks]
    doc = render_brief(idea, brief, kit, clean, (before[1], after[1], after[2]), report, checks, plan_checks,
                       batch_id=batch_id, build_no=build_no)
    return {"brief": brief, "kit": kit, "doc": doc, "score": after[1],
            "warnings": brief_warnings(idea, brief)}


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


# ---------- step 2b: build options for the picked idea ----------

def _idea_from_option(idea: dict, o: dict, research: dict | None = None) -> dict:
    """The picked option replaces the rough plan; topic, sources and checks stay."""
    return {**idea,
            "title": o["title"], "format": o["kind"], "role_angle": o["why_hiring_cares"],
            "build_plan": o["what_you_build"] + " Steps: " + " ".join(
                f"({n}) {s}" for n, s in enumerate(o.get("steps_preview", []), 1)),
            "build_minutes": o["minutes"], "hook_options": [o["hook"]], "visual": o["post_shows"],
            "needs": o.get("needs", []), "models_to_run": o.get("models_to_run", []),
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


def _next_tag(existing: dict | None) -> str:
    """'' for the first options list of an idea, then 'b', 'c', ... for fresh lists (#N.3b)."""
    if not existing:
        return ""
    tag = existing.get("tag", "")
    if tag == "z":       # the email tag allows a-z; past that a reply could never be matched
        raise ValueError("this idea already has 25 fresh options lists; pick one of them or another idea")
    return "b" if not tag else chr(ord(tag) + 1)


def _send_options(batch_id: int, choice: int, idea: dict, result: dict, send_email: bool) -> None:
    tag = result.get("tag", "")
    text = render_options(batch_id, choice, idea, result)
    ideas_dir = settings.output_dir / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    (ideas_dir / f"{date.today()}-#{batch_id}.{choice}{tag}-options.md").write_text(text, encoding="utf-8")
    (ideas_dir / f"{date.today()}-#{batch_id}.{choice}{tag}-options-full.md").write_text(
        render_options_full(batch_id, choice, idea, result), encoding="utf-8")
    if send_email and settings.has_email:
        mailer.send(mailer.subject_for(batch_id, f"Build options: {idea['title'][:60]}", idea=choice,
                                       version=tag), text, html=text_email(text))
    else:
        log.info("options saved to %s", ideas_dir)


def propose(batch_id: int, choice: int, send_email: bool = True, fresh: bool = False,
            reply_id: str | None = None) -> dict:
    """The person picked idea `choice`: offer 3-5 concrete builds on it, fact-checked and priced.
    If options already exist they are resent unchanged (D4), so every options email keeps
    pointing at the list it showed; fresh=True ("3 new", --fresh) makes a new tagged list (D23).
    reply_id: the email that asked. If that same email already made the current list (a redo
    after a crash), the list is resent instead of paying for another one."""
    row = db.get_batch(batch_id)
    if not row:
        raise ValueError(f"No batch #{batch_id}")
    data = json.loads(row["ideas_json"])
    if not 1 <= choice <= len(data["ideas"]):
        raise ValueError(f"Batch #{batch_id} has ideas 1-{len(data['ideas'])}")
    idea = data["ideas"][choice - 1]
    existing = (data.get("options") or {}).get(str(choice))
    redo = bool(existing and reply_id and existing.get("from_reply") == reply_id)
    if existing and (not fresh or redo):
        log.info("options for #%d idea %d already exist; resending them", batch_id, choice)
        _send_options(batch_id, choice, idea, existing, send_email)
        return existing
    if fresh:
        _next_tag(existing)                  # refuse past 'z' before paying for a list
    log.info("options for #%d idea %d: %s", batch_id, choice, idea["title"])
    result, _ = _make_options(idea)
    if reply_id:
        result["from_reply"] = reply_id

    def save(d: dict) -> None:
        # D26: merge just this idea's options into the latest saved data. The tag is decided here,
        # under the write lock, so two fresh lists made at once never share a tag.
        cur = d.setdefault("options", {}).get(str(choice))
        same_reply = bool(cur and reply_id and cur.get("from_reply") == reply_id)
        result["tag"] = _next_tag(cur) if fresh and not same_reply else (cur or {}).get("tag", "")
        if cur and (not fresh or same_reply):     # another request saved a list first: keep that one
            result.clear()
            result.update(cur)
            return
        d["options"][str(choice)] = result
    db.edit_batch(batch_id, save, status="options_sent")
    _send_options(batch_id, choice, idea, result, send_email)
    return result


def _make_options(idea: dict) -> tuple[dict, list[str]]:
    """Research the hot thing and write 3-5 fact-checked, priced build options for it.
    No saving, no email: propose() and the eval both use this. Returns (result, dropped)."""
    llm.reset_usage()
    cat = factgate.load_catalogs()
    keys = [m.key for m in factgate.shortlist(cat, idea.get("entities", []) + [idea.get("handle", "")])]
    notes = _model_notes(cat, keys)

    found = sources.research(idea)
    digest = sources.research_digest(found)
    if found.get("docs"):
        digest += (f"\n\nOfficial docs ({found['docs_from']}): what it is for and how it is used:\n"
                   + found["docs"][:2500])
    known = [t.lower() for t in sources.research_titles(found)]

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
    return result, dropped


def _overlap(a: str, b: str) -> bool:
    """Does text a quote source b? (3+ shared distinctive words)"""
    words = lambda s: {w for w in re.findall(r"[a-z0-9]{3,}", s)} - {"the", "and", "for", "with", "views", "stars"}
    return len(words(a) & words(b)) >= 3


REQUIRED_KINDS = ("demo-app", "open-source-tool")   # every options email has both
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
    for batch_id, idea_ref, choice, uid in mailer.fetch_replies():
        # handle_reply catches its own errors and emails you about them; only if it returns
        # (success or emailed failure) is the reply recorded as handled and marked read. A crash
        # or kill leaves it unread, so the next check redoes it, and a finished build is resent,
        # not rebuilt (D8, D18). Each reply is handled at most once, even if marking it read
        # fails, and one bad reply never blocks the ones after it.
        key = "reply:" + (uid.decode() if isinstance(uid, bytes) else str(uid))
        try:
            state = db.claim(key)            # atomic: `run` and a manual `check` never both pay
            if state == "busy":
                continue
            if state == "claimed":
                try:
                    handle_reply(batch_id, idea_ref, choice, reply_id=key)
                except BaseException:
                    db.release(key, done=False)          # not finished: the next check redoes it
                    raise
                db.release(key, done=True)
            mailer.mark_read([uid])
        except Exception:
            log.exception("reply %s to #%d could not be finished; will retry next check", key, batch_id)


def _split_ref(idea_ref) -> tuple[int | None, str]:
    """3 -> (3, ''); '3b' -> (3, 'b'); None -> (None, '')."""
    if idea_ref is None:
        return None, ""
    m = re.fullmatch(r"(\d+)([a-z]?)", str(idea_ref))
    return (int(m.group(1)), m.group(2)) if m else (None, "")


def handle_reply(batch_id: int, idea_ref: int | str | None, choice: str, reply_id: str | None = None) -> None:
    """Reply to the ideas email: "3" -> options (saved ones are resent), "3 new" -> fresh options,
    "3 go" -> build the top option now, "skip". Reply to an options email ("#8.3" or "#8.3b"):
    "2" or "2 go" -> build that option, "2 new" -> a fresh options list for that idea; if that
    list was replaced by a newer one, say so instead. A pick on a skipped batch un-skips it."""
    row = db.get_batch(batch_id)
    if not row:
        return
    idea_n, version = _split_ref(idea_ref)
    if choice == "skip":
        if idea_n is None and row["status"] != "skipped":
            db.update_batch(batch_id, status="skipped")       # ignored if something was built
            log.info("batch #%d skip reply handled", batch_id)
        return
    word = choice.split()[1] if len(choice.split()) > 1 else ""
    num = int(choice.split()[0])
    try:
        if idea_n is not None:
            saved = (json.loads(row["ideas_json"]).get("options") or {}).get(str(idea_n))
            redo = bool(reply_id and saved and saved.get("from_reply") == reply_id)   # this reply made it
            if saved and saved.get("tag", "") != version and not redo:
                newest = f"#{batch_id}.{idea_n}{saved.get('tag', '')}"
                log.info("reply to replaced options list #%d.%d%s; newest is %s", batch_id, idea_n, version, newest)
                _notify(mailer.subject_for(batch_id, "That options list was replaced", idea=idea_n,
                                           version=version, kind="note"),
                        f"You asked for fresh options after this email, so its list was replaced.\n"
                        f"Reply to the newest options email ([PostPilot {newest}]) to pick a build.")
                return
            if word == "new":
                propose(batch_id, idea_n, fresh=True, reply_id=reply_id)
            else:
                build(batch_id, idea_n, option=num)
        elif word == "go":
            res = propose(batch_id, num, send_email=False)
            build(batch_id, num, option=res["recommended"])
        else:
            propose(batch_id, num, fresh=(word == "new"), reply_id=reply_id)
    except Exception as e:
        log.exception("reply handling failed for #%d", batch_id)
        _notify(mailer.subject_for(batch_id, "Something failed", idea=idea_n, version=version),
                f"Error: {e}\nReply to this email with the same answer to try again.")


def _notify(subject: str, body: str) -> None:
    """A short status email. A failure to send it is logged, never raised: the reply it is about
    has been handled either way."""
    if not settings.has_email:
        return
    try:
        mailer.send(subject, body)
    except Exception:
        log.exception("could not send: %s", subject)


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


# ---------- build eval: options -> BUILD.md -> post, scored against fixed bars (D14, D24, D25) ----------

EVAL_BUDGET = 60_000        # GPT-6 tokens one eval run may spend (D24)
LIVE_RESERVE = 100_000      # never let an eval leave the live loop with less than this today
EVAL_MIN = 20_000           # below this an eval can't finish one story: don't start
EVAL_BARS = {
    "options dropped <= 1": lambda r: r["dropped"] <= 1,
    "demo + GitHub repo offered": lambda r: r["has_required"],
    "BUILD.md warnings = 0": lambda r: r["warnings"] == 0,
    f"post human score >= {POST_TARGET}": lambda r: r["post_score"] >= POST_TARGET,
}


def _eval_stories(n: int) -> list[tuple[int, int, dict]]:
    """The last n saved stories: ideas that got options first (what you actually picked), then
    the first idea of recent batches."""
    picked, fallback = [], []
    for b in db.recent_batches(30):
        data = json.loads(b["ideas_json"])
        ideas = data.get("ideas", [])
        for k in (data.get("options") or {}):
            if k.isdigit() and 1 <= int(k) <= len(ideas):
                picked.append((b["id"], int(k), ideas[int(k) - 1]))
        if ideas:
            fallback.append((b["id"], 1, ideas[0]))
    seen, out = set(), []
    for item in picked + fallback:
        if (item[0], item[1]) not in seen:
            seen.add((item[0], item[1]))
            out.append(item)
    return out[:n]


def eval_builds(n: int = 2) -> dict:
    """Re-run saved stories through the CURRENT options, BUILD.md and post prompts with GPT-6 and
    score each against EVAL_BARS. Never saves or emails anything. Run it by hand after changing a
    prompt; compares with the previous run."""
    stories = _eval_stories(n)
    if not stories:
        print("No saved stories yet. Run `python -m postpilot scout --no-email` first.")
        return {}
    used, cap = llm.paid_tokens_today(), llm.daily_cap()
    reserve = min(LIVE_RESERVE, cap // 2)          # a small cap still leaves half for live emails
    allowed = min(EVAL_BUDGET, cap - reserve - used)
    if allowed < EVAL_MIN:
        print(f"Not running: today's GPT-6 use is {used:,} of {cap:,}, and {reserve:,} stays reserved for "
              f"your live emails, so an eval would get under {EVAL_MIN:,} tokens. Try again tomorrow.")
        return {"refused": True}
    rows = []
    with llm.token_budget(allowed) as budget:
        for bid, n_idea, idea in stories:
            try:
                res, dropped = _make_options(idea)
                opt = res["options"][res["recommended"] - 1]
                plan = _make_plan(_idea_from_option(idea, opt, res.get("research")))
            except llm.BudgetExceeded:
                print(f"Token budget reached ({allowed:,}); stopping early.")
                break
            except Exception as e:
                print(f"#{bid}.{n_idea} ERROR {e}")
                continue
            r = {"story": f"#{bid}.{n_idea}", "title": idea.get("title", "")[:45], "dropped": len(dropped),
                 "has_required": all(any(o["kind"] == k for o in res["options"]) for k in REQUIRED_KINDS),
                 "warnings": len(plan["warnings"]), "post_score": round(plan["score"])}
            r["bars"] = {name: bool(test(r)) for name, test in EVAL_BARS.items()}
            rows.append(r)
            print(f"{r['story']:8} {r['title']:45}  dropped {r['dropped']}  warnings {r['warnings']}  "
                  f"post {r['post_score']}  " + " ".join("PASS" if ok else "FAIL" for ok in r["bars"].values()))
        spent = budget["used"]
    if not rows:
        return {"stories": 0, "tokens": spent}
    previous = json.loads(db.get_meta("eval:builds:last", "{}") or "{}")
    summary = {name: sum(r["bars"][name] for r in rows) / len(rows) for name in EVAL_BARS}
    print(f"\nBars ({len(rows)} stories, {spent:,} GPT-6 tokens):")
    for name, rate in summary.items():
        before = previous.get(name)
        trend = "" if before is None else ("  (same as last run)" if before == rate else
                                           f"  ({'better' if rate > before else 'WORSE'} than last run: {before:.0%})")
        print(f"  {'PASS' if rate == 1 else 'FAIL'}  {name:28} {rate:.0%} of stories{trend}")
    db.set_meta("eval:builds:last", json.dumps(summary))
    return {"stories": len(rows), "tokens": spent, "bars": summary, "rows": rows}


# ---------- outcomes: what your posts actually did (eng review O2) ----------

def _built_item(batch, build_no: int) -> tuple[dict, str]:
    """(idea, what was built) for one outcome row: the built option's type when the build came
    from an options email, else the idea's own format. build_no 0 = an old batch's chosen idea."""
    data = json.loads(batch["ideas_json"]) if batch else {}
    ideas, builds = data.get("ideas", []), data.get("builds", [])
    if build_no and 1 <= build_no <= len(builds):
        rec = builds[build_no - 1]
        idea = ideas[rec["idea"] - 1] if 1 <= rec["idea"] <= len(ideas) else {}
        if rec.get("kind"):                  # saved at build time, so a later fresh list can't change it
            return idea, rec["kind"]
        listing = (data.get("options") or {}).get(str(rec["idea"]), {})
        opts = listing.get("options", []) if listing.get("tag", "") == rec.get("tag", "") else []
        if rec.get("option") and 1 <= rec["option"] <= len(opts):
            return idea, opts[rec["option"] - 1].get("kind", "?")
        return idea, idea.get("format", "?")
    chosen = batch["chosen"] if batch else None
    idea = ideas[chosen - 1] if chosen and 1 <= chosen <= len(ideas) else {}
    return idea, idea.get("format", "?")


def report() -> dict:
    """Rank what you built (option type) and topic kinds by inbound (recruiters + founders +
    other) per post. Each post is credited to the exact build you posted (D20)."""
    rows = db.all_outcomes()
    posted = [r for r in rows if r["posted_url"]]
    if not posted:
        print("No posts logged yet. After posting: python -m postpilot posted <batch> <linkedin-url>")
        return {}
    by = {"built": {}, "topic kind": {}}
    total_in = 0
    print(f"{'post':>8}  {'inbound':>7}  {'recr':>4} {'fndr':>4} {'othr':>4}  built / topic")
    for r in posted:
        batch = db.get_batch(r["batch_id"])
        idea, built = _built_item(batch, r["build_no"])
        inbound = (r["recruiters"] or 0) + (r["founders"] or 0) + (r["other"] or 0)
        total_in += inbound
        kind = idea.get("topic_kind", "?") or "?"
        for dim, key in (("built", built), ("topic kind", kind)):
            agg = by[dim].setdefault(key, {"posts": 0, "inbound": 0})
            agg["posts"] += 1
            agg["inbound"] += inbound
        label = f"#{r['batch_id']}" + (f".b{r['build_no']}" if r["build_no"] else "")
        print(f"{label:>8}  {inbound:>7}  {r['recruiters'] or 0:>4} {r['founders'] or 0:>4} "
              f"{r['other'] or 0:>4}  {built} / {idea.get('topic', '?')[:50]}")
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
