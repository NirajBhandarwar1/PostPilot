"""The cycle: scout -> fact-gated ideas -> email -> (your reply) -> build -> email the draft.

          sources.collect()                         ~130 live signals
                 │
          brain.find_topics()  ─► ranking.heat() ─► filter_links / dedupe / history ─► chosen[≤3]
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
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import brain, db, examples, factgate, llm, mailer, ranking, sources
from .config import ROOT, settings
from .render import (
    CURSOR_PROMPT,
    brief_warnings,
    markdown_email,
    render_brief,
    render_ideas,
    render_options,
    render_options_full,
    text_email,
)
from .render import slug as _slug
from .vendor import detect as detector
from .vendor import humanize as humanizer

log = logging.getLogger("postpilot")
HOOKS = json.loads((ROOT / "postpilot" / "vendor" / "hooks.json").read_text(encoding="utf-8"))
LEXICON = humanizer.load_lexicon()


def voice() -> str:
    try:
        text = settings.voice_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    # An untouched template has empty answers; treat it as missing.
    # Answers in (brackets) are the template's hints, not real answers.
    filled = [ln for ln in text.splitlines() if re.match(r"^- \*\*.+:\*\*\s*[^\s(]", ln)]
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
    cutoff = datetime.now(UTC).timestamp() - HISTORY_DAYS * 86400
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


def generate_batch(signals: list[dict], history: list, voice: str, cat: factgate.Catalog,
                   hf_lookup=None, gh_lookup=None) -> dict | None:
    """Find hot topics, write one idea per topic, fact-check each idea, regenerate a failing
    idea once, drop it if it still fails. Returns the batch dict, or None if no idea passed."""
    topics = [t.model_dump() for t in brain.find_topics(signals).topics]
    for t in topics:
        t["link_ids"] = factgate.filter_links(t, signals)   # sources that really name the topic
        t["heat"], t["heat_where"] = ranking.heat(t, signals, t["link_ids"])
        t["evidence"] = [f"[{signals[i]['kind']}] {signals[i]['title']} ({signals[i]['detail']})"
                         for i in t["link_ids"][:6]]
    # Measured heat leads; the model's opinion (builder relevance, buzz) is capped (eng review O3).
    for t in topics:
        _check_handle(t, gh_lookup)
        t["score"] = (t["heat"] + min(ranking.MODEL_SCORE_CAP, t.get("builder_relevance", 5) + 0.5 * t.get("buzz", 5))
                      + ranking.HANDLE_BONUS.get(t.get("handle_type", "news"), 0.0))
    topics.sort(key=lambda t: -t["score"])
    for t in topics:
        log.info("topic %-55s heat %4.0f buzz %2d links %d  %s", t["name"][:55], t["heat"],
                 t["buzz"], len(t["link_ids"]), t["heat_where"][:60])

    fresh: list[dict] = []
    used_ids: set[int] = set()
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
        assert idea is not None   # a missing idea gave Verdict(False) above and was dropped
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
        assert idea is not None   # a missing idea gave Verdict(False) above and was dropped
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
        **_writer_facts(),
    }


def _writer_facts() -> dict:
    """Which models answered and which provider was expected to write. Saved with the batch or plan, so the
    'a backup model wrote this' note prints the same way every time, with no live model state at print time."""
    primary = next(iter(llm.providers("writer")), None)
    return {"writers": sorted(llm.USAGE), "expected": primary.name if primary else ""}


def save_run(signals: list[dict], history: list, cat: factgate.Catalog) -> Path:
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
    batch = generate_batch(signals, history, voice(), cat, hf_lookup=factgate.hf_model,
                           gh_lookup=factgate.github_repo)
    db.set_meta("last_scout_at", db.now_iso())
    if batch:
        snap = json.loads(run_path.read_text(encoding="utf-8"))
        snap["stats"] = batch["stats"]
        run_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    if not batch or not batch["ideas"]:
        log.warning("no ideas passed this run; nothing saved or sent")
        llm.log_health(batch["stats"] if batch else {"pitched": 0, "dropped": 0})
        return None

    batch_id = db.add_batch(batch, status="draft")
    text = render_ideas(batch_id, batch)
    (ideas_dir / f"{date.today()}-#{batch_id}.md").write_text(text, encoding="utf-8")
    st = batch["stats"]
    log.info("batch #%d: %d pitched, %d failed first check, %d regenerated, %d dropped, %d unverified",
             batch_id, st["pitched"], st["failed_first"], st["regenerated"], st["dropped"], st["unverified"])
    for model, u in llm.USAGE.items():
        log.info("tokens %s: %d calls, %d in, %d out", model, u["calls"], u["in"], u["out"])
    llm.log_health(st)

    if send_email and settings.has_email:
        _send_ideas(batch_id, text)
    else:
        log.info("email not configured or disabled: ideas saved to %s", ideas_dir)
    return batch_id


def _send_ideas(batch_id: int, text: str) -> bool:
    """Email the ideas. The ideas are already paid for and saved, so a failed send (wrong App
    Password, no network) never raises: the batch is marked unsent and resend_unsent() retries
    only the email. Raising here would make the loop buy a whole new scout every hour."""
    try:
        mailer.send(mailer.subject_for(batch_id, "What's hot in AI: reply with a number"), text,
                    html=text_email(text))
    except Exception:
        log.exception("could not email batch #%d; the ideas are saved and the email will be retried", batch_id)
        db.edit_batch(batch_id, lambda d: d.__setitem__("unsent", True))
        return False
    db.edit_batch(batch_id, lambda d: d.pop("unsent", None), status="sent")
    return True


def resend_unsent() -> None:
    """Retry the ideas email of the newest batch whose send failed. Older failed batches are
    stale (a newer scout replaced them) and stay saved as drafts."""
    if not settings.has_email:
        return
    for b in db.recent_batches(5):
        data = json.loads(b["ideas_json"])
        if data.get("unsent") and b["status"] == "draft":
            _send_ideas(b["id"], render_ideas(b["id"], data))
            return


# ---------- step 2: build ----------

def _plan_glance(doc: str) -> str:
    """One line about the plan, read from BUILD.md itself so a resent plan gets the same summary."""
    bits = []
    story = re.search(r"Type: ([^|\n]+?)\s*\|\s*Difficulty: ([^|\n]+?)\s*\|\s*Realistic time: ([^\n]+)", doc)
    if story:
        bits += [story.group(1).strip(), re.split(r" [–-] ", story.group(2).strip())[0],
                 "about " + story.group(3).strip().lstrip("~")]
    steps = re.findall(r"^### Step \d+ (🤖|🧑)", doc, re.M)
    if steps:
        bits.append(f"{len(steps)} steps ({steps.count('🧑')} are yours to do)")
    score = re.search(r"Human score: (\d+)/100", doc)
    if score:
        bits.append(f"post score {score.group(1)}/100")
    return " · ".join(bits)


def _checks_count(doc: str) -> int:
    """How many things BUILD.md asks you to verify (before building and before posting)."""
    n = 0
    for pattern in (r"## Check before building\n(.*?)\n(?:Cursor: stop|#)", r"Check before you post\n(.*?)\n(?:## )?\d\. After you post"):
        m = re.search(pattern, doc, re.S)
        if m:
            n += len([ln for ln in m.group(1).splitlines() if ln.strip()])
    return n


def _email_build(batch_id: int, choice: int, title: str, summary: str, doc: str) -> None:
    """A short email: the plan lives in the BUILD.md attachment, so nobody has to read it in the mail."""
    if not settings.has_email:
        return
    lines = [summary, "", "Your full plan is attached as BUILD.md: the steps for Cursor, the demo shot list "
             "and your post draft."]
    glance = _plan_glance(doc)
    if glance:
        lines += ["", f"At a glance: {glance}"]
    checks = _checks_count(doc)
    if checks:
        lines.append(f"Check first: {checks} thing(s) to verify before you build or post (listed in BUILD.md).")
    lines += ["", "How to use it:",
              "1. Open BUILD.md and do its 'Before you start' list. Then make an empty folder, open it in "
              "Cursor and put BUILD.md in it.",
              f"2. In Cursor's Agent chat, paste: {CURSOR_PROMPT}",
              "3. When it works, follow Part B of BUILD.md to record the demo and post."]
    intro = "\n".join(lines) + "\n"
    # kind="plan": a reply to this email ("step 4 failed") is never read as a pick.
    mailer.send(mailer.subject_for(batch_id, f"Build plan ready: {title[:60]}", idea=choice, kind="plan"),
                intro, html=markdown_email(intro, ""), attachments=[("BUILD.md", doc)])


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
        idea = idea_from_option(idea, opts[option - 1], listing.get("research"), listing.get("evidence_pack"))
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
    plan = make_plan(idea, batch_id=batch_id, build_no=predicted)
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


def make_plan(idea: dict, batch_id: int | None = None, build_no: int | None = None) -> dict:
    """Write the BUILD.md plan and post for one idea (or picked option). No saving, no email:
    build() and the eval both use this, so the eval measures exactly what you would get."""
    llm.reset_usage()                       # so the plan can say which model wrote it
    found = idea.get("research") or {}
    docs, docs_from = found.get("docs", ""), found.get("docs_from", "")
    if not docs:
        docs, docs_from = sources.docs_for(idea)
    pack_text = _pack_text(idea)
    if pack_text:       # the saved fact list replaces the raw digest; only a short docs excerpt stays
        brief = brain.build_brief(idea, pack_text, docs[:DOCS_WITH_PACK_CHARS], docs_from)
    else:               # ideas saved before the evidence pack existed
        brief = brain.build_brief(idea, sources.research_digest(found), docs[:DOCS_RAW_CHARS], docs_from)

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
    facts = _writer_facts()
    doc = render_brief(idea, brief, kit, clean, (before[1], after[1], after[2]), report, checks, plan_checks,
                       batch_id=batch_id, build_no=build_no, writers=facts["writers"], expected=facts["expected"])
    return {"brief": brief, "kit": kit, "doc": doc, "score": after[1],
            "warnings": brief_warnings(idea, brief), **facts}


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


USE_EVIDENCE_PACK = True      # off = the old raw-digest prompts (the size benchmark compares both)
DOCS_WITH_PACK_CHARS = 2000   # the pack already carries the docs' key facts; build_brief keeps a short excerpt
DOCS_RAW_CHARS = 6000
DIGEST_RAW_CHARS = 4000


def _pack_text(idea: dict, skip: tuple[str, ...] = ()) -> str:
    """The saved evidence pack of this idea as prompt text, without facts from the `skip` sources; '' for ideas
    saved without one (or a broken one)."""
    raw = idea.get("evidence_pack")
    if not raw:
        return ""
    try:
        return brain.render_evidence(brain.EvidencePack.model_validate(raw), skip)
    except Exception as e:
        log.warning("saved evidence pack is unreadable (%s); using the raw research", e)
        return ""


def _make_pack(idea: dict, digest: str, docs: str) -> dict | None:
    """Build the evidence pack once per options request. Never fatal: without it the prompts fall back to
    the raw digest. The fact gate and scans keep reading the raw sources and titles, not this pack."""
    if not USE_EVIDENCE_PACK:
        return None
    try:
        pack = brain.make_evidence(idea, digest, docs)
    except Exception as e:
        log.warning("evidence pack failed (%s); using the raw research digest", e)
        return None
    if not pack.facts:
        log.warning("evidence pack came back with no facts; using the raw research digest")
        return None
    return pack.model_dump()


def _facts(idea: dict) -> str:
    """Numbers the post may state as fact. Facts that came from LinkedIn posts are left out: a stranger's claim is
    not a source ('cut my costs 80%'), it only helps the options pick a fresh angle."""
    pack_text = _pack_text(idea, skip=("linkedin",))
    if pack_text:
        return "\n".join([pack_text, f"- {idea['handle_facts']}"] if idea.get("handle_facts") else [pack_text])
    r = idea.get("research") or {}
    out = [f"- YouTube video \"{v['title']}\": {v['views']:,} views" for v in r.get("videos", [])[:3]]
    out += [f"- GitHub repo {g['title'].split(':')[0]}: {g['stars']:,} stars" for g in r.get("repos", [])[:3]]
    out += [f"- Hacker News thread \"{h['title']}\": {h['points']} points, {h['comments']} comments"
            for h in r.get("hn", [])[:2]]
    if idea.get("handle_facts"):
        out.append(f"- {idea['handle_facts']}")
    return "\n".join(out)


def _write_post(idea: dict, plan: str = ""):
    """Post draft -> humanizer -> score. While it is under target, rewrite the BEST draft so far with
    the exact failing checks as feedback (up to POST_TRIES drafts) and keep the best one. Measured on
    the free models: a rewrite that starts over often scores lower, so each rewrite edits the best
    text, and the loop stops after the first rewrite that does not beat it (no wasted tokens)."""
    best, feedback, previous, facts = None, "", "", _facts(idea)
    picked = examples.chosen(idea)
    ex = examples.for_prompt(idea, picked)   # '' without an examples file: then the prompt is exactly as before
    short_ex = examples.for_prompt(idea, picked[:2], chars=350)    # a rewrite also carries the draft: keep Groq in reach
    for attempt in range(POST_TRIES):
        use = short_ex if previous else ex
        kit = brain.build_post(idea, voice(), HOOKS, feedback=feedback, plan=plan, facts=facts,
                               previous=previous, **({"examples": use} if use else {}))
        clean, report = humanizer.humanize(kit.post_draft, LEXICON)
        dup = examples.shared_run(clean, picked)
        if dup:                              # a phrase copied from an example never ships: rewrite it
            log.info("post draft %d copies a phrase from an example (%r)", attempt + 1, dup)
            if attempt + 1 < POST_TRIES:
                previous = clean
                feedback = (f"REWRITE: your draft repeats this phrase from a style example: \"{dup}\". "
                            "Say it in your own words; keep everything else.")
                continue
            if best is not None:
                break                        # out of tries: keep the earlier draft, which did not copy
        nums = examples.copied_numbers(clean, picked, facts)
        if nums and not dup and attempt + 1 < POST_TRIES:
            previous = clean
            feedback = (f"REWRITE: your draft uses numbers from a style example, not from the facts: {', '.join(nums)}. "
                        "Remove them or use only numbers from the facts; keep everything else.")
            continue
        before, after = _score(kit.post_draft), _score(clean)
        improved = best is None or after[1] > best[4][1]
        if improved:
            best = (kit, clean, report, before, after)
        if after[1] >= POST_TARGET and after[2] == "PASS":
            break
        assert best is not None
        if not improved:
            log.info("post rewrite %d scored %.0f, not above the best %.0f; keeping the best",
                     attempt + 1, after[1], best[4][1])
            break
        assert best is not None
        kit, clean, _r, _b, after = best
        weak = [(n, d) for n, (sc, d) in sorted(after[0].items(), key=lambda kv: kv[1][0]) if sc < 75][:3]
        previous = clean
        feedback = (f"REWRITE: the draft below scored {after[1]:.0f}/100 on the human-writing check "
                    f"(target {POST_TARGET}). Fix exactly these: "
                    + "; ".join(f"{n.lower()}: {d}" for n, d in weak)
                    + ". Edit the draft: keep its facts, structure and placeholders; change the wording.")
        log.info("post draft %d scored %.0f (%s)%s", attempt + 1, after[1], after[2],
                 "; rewriting" if attempt + 1 < POST_TRIES else "; keeping the best")
    if best is not None and examples.shared_run(best[1], picked):    # every draft copied: ship it, but say so
        best[0].fact_checks = [*best[0].fact_checks,
                               "The draft repeats a phrase from a style example: reword it before you post."]
    if best is not None and (nums := examples.copied_numbers(best[1], picked, facts)):
        best[0].fact_checks = [*best[0].fact_checks,
                               f"Numbers copied from a style example, not your facts: {', '.join(nums)}. Verify or remove."]
    return best


# ---------- step 2b: build options for the picked idea ----------

def idea_from_option(idea: dict, o: dict, research: dict | None = None, pack: dict | None = None) -> dict:
    """The picked option replaces the rough plan; topic, sources and checks stay."""
    return {**idea, **({"evidence_pack": pack} if pack else {}),
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
    v = factgate.check_idea(as_idea, topic, sigs, cat)
    if v.ok:                     # probe only options that survived: the cache makes repeats free
        for key in dict.fromkeys(o.get("models_to_run") or []):
            provider, _, model = key.partition(":")
            ok, why = llm.probe_model(provider, model)
            if not ok:       # keep the option, but say plainly that nobody has confirmed this model works
                v.unverified.append(f"Need to check: could not confirm model '{key}' works right now ({why}). "
                                    "Try one call with it before you build on it.")
    return v


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
    result, _ = make_options(idea)
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


def make_options(idea: dict) -> tuple[dict, list[str]]:
    """Research the hot thing and write 3-5 fact-checked, priced build options for it.
    No saving, no email: propose() and the eval both use this. Returns (result, dropped)."""
    llm.reset_usage()
    cat = factgate.load_catalogs()
    keys = [m.key for m in factgate.shortlist(cat, [*idea.get("entities", []), idea.get("handle", "")])]
    notes = _model_notes(cat, keys)

    found = sources.research(idea)
    digest = sources.research_digest(found)
    docs = found.get("docs", "")
    pack = _make_pack(idea, digest, docs[:2500])
    if pack:
        digest = brain.render_evidence(brain.EvidencePack.model_validate(pack))
    else:
        digest = digest[:DIGEST_RAW_CHARS]
        if docs:
            digest += (f"\n\nOfficial docs ({found['docs_from']}): what it is for and how it is used:\n"
                       + docs[:2500])
    known = [t.lower() for t in sources.research_titles(found)]

    kept: list[dict] = []
    raw: dict = {}
    dropped: list = []
    extra = ""
    for _attempt in range(2):     # one retry if too few options pass, or no "look what I built" demo
        raw = brain.make_options(idea, voice(), keys, notes, research_text=digest, extra=extra).model_dump()
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
    result = {"options": kept, "recommended": rec or 1, "research": found, "evidence_pack": pack,
              "story_intro": raw.get("story_intro", ""),
              "recommended_reason": reason if rec else "Best remaining option after the fact check.",
              **_writer_facts()}
    return result, dropped


def _overlap(a: str, b: str) -> bool:
    """Does text a quote source b? (3+ shared distinctive words)"""
    def words(s):
        return set(re.findall(r"[a-z0-9]{3,}", s)) - {"the", "and", "for", "with", "views", "stars"}
    return len(words(a) & words(b)) >= 3


REQUIRED_KINDS = ("demo-app", "open-source-tool")   # every options email has both
# ---------- the always-on loop ----------

RETRY_AFTER_FAILURE_S = 3600
ZERO_IDEA_RETRY_S = 3 * 3600          # a scout that found no ideas is tried again in 3 hours...
ZERO_IDEA_RETRIES_PER_DAY = 2         # ...at most twice a UTC day, so a dry free quota is not hammered
LOOP_DOWN_AFTER_S = 3600              # no sign of life for this long: the loop was not running


def schedule_zero_idea_retry(now: datetime | None = None) -> bool:
    """After a scout with no ideas, plan another one in 3 hours, at most twice per UTC day.
    Returns True when a retry was planned. Set by run_forever AFTER it clears retry_scout_at."""
    now = now or datetime.now(UTC)
    key = "scout_retries:" + now.strftime("%Y-%m-%d")
    used = int(db.get_meta(key, "0") or 0)
    if used >= ZERO_IDEA_RETRIES_PER_DAY:
        log.warning("scout found no ideas again; no more retries today (%d already)", used)
        return False
    db.set_meta(key, str(used + 1))
    at = now + timedelta(seconds=ZERO_IDEA_RETRY_S)
    db.set_meta("retry_scout_at", at.isoformat(timespec="seconds"))
    log.warning("scout found no ideas; retrying at %s (retry %d of %d today)",
                at.isoformat(timespec="minutes"), used + 1, ZERO_IDEA_RETRIES_PER_DAY)
    return True


def heartbeat(now: datetime | None = None) -> None:
    """Write 'the loop is alive' so `status` and the next start can tell whether it was running. Never raises."""
    try:
        db.set_meta("last_loop_at", (now or datetime.now(UTC)).isoformat(timespec="seconds"))
    except Exception as e:
        log.warning("could not write the loop heartbeat: %s", str(e)[:200])


def loop_age(now: datetime | None = None) -> float | None:
    """Seconds since the loop last wrote its heartbeat, or None if it never has."""
    last = db.get_meta("last_loop_at")
    if not last:
        return None
    return ((now or datetime.now(UTC)) - datetime.fromisoformat(last)).total_seconds()


def _ago(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def loop_line(now: datetime | None = None) -> str:
    """The `status` line: alive, STOPPED?, or never started."""
    age = loop_age(now)
    if age is None:
        return "never started (python -m postpilot run)"
    if age > LOOP_DOWN_AFTER_S:
        return f"STOPPED? last seen {_ago(age)} ago (start it again; README, 'Keep it running')"
    return f"alive, last seen {_ago(age)} ago"


def report_downtime(gap_s: float, now: datetime | None = None) -> None:
    """Email once that the loop was not running. A failed send is logged and never stops the start."""
    now = now or datetime.now(UTC)
    last = now - timedelta(seconds=gap_s)
    try:
        _notify(f"[PostPilot] was down for {_ago(gap_s)}",
                f"PostPilot was not running for {_ago(gap_s)} (last seen {last:%Y-%m-%d %H:%M} UTC, back at "
                f"{now:%Y-%m-%d %H:%M} UTC).\n\nThe scout and the reply checks are running again; a missed scout "
                "runs by itself. If this happens often, start PostPilot at logon: see 'Keep it running' in the README.")
    except Exception:
        log.exception("could not send the 'was down' email")


def scout_due() -> bool:
    retry = db.get_meta("retry_scout_at")
    if retry:
        return datetime.now(UTC) >= datetime.fromisoformat(retry)
    last = db.get_meta("last_scout_at")
    if not last:
        return True
    age = datetime.now(UTC) - datetime.fromisoformat(last)
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
    """A short status email. If it can't be sent the error escapes: check_replies then leaves the
    reply unhandled and retries it (saved lists and plans are resent, nothing is paid twice), so
    a failed build never ends in silence."""
    if settings.has_email:
        mailer.send(subject, body)


def run_forever() -> None:
    if not llm.available():
        raise SystemExit("Set up a model provider in .env (e.g. GROQ_API_KEY) before starting the loop.")
    log.info("PostPilot running: scout every %g days, check email every %g min",
             settings.cycle_days, settings.check_minutes)
    gap = loop_age()                        # before the first mark of this run: how long was nothing running?
    heartbeat()
    if gap is not None and gap > LOOP_DOWN_AFTER_S:
        report_downtime(gap)
    while True:
        heartbeat()
        try:
            if scout_due():
                batch_id = scout()
                db.set_meta("retry_scout_at", "")
                if batch_id is None:        # no ideas and no error: plan the capped retry after the clear above
                    schedule_zero_idea_retry()
        except Exception:
            retry_at = datetime.fromtimestamp(time.time() + RETRY_AFTER_FAILURE_S, UTC)
            log.exception("scout failed; retrying at %s", retry_at.isoformat(timespec="minutes"))
            db.set_meta("retry_scout_at", retry_at.isoformat(timespec="seconds"))
        heartbeat()
        try:
            resend_unsent()
        except Exception:
            log.exception("resending the ideas email failed")
        heartbeat()
        try:
            check_replies()
        except Exception:
            log.exception("reply check failed")
        heartbeat()
        time.sleep(settings.check_minutes * 60)
