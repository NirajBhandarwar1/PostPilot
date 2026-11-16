"""What you can read back: the replay of saved scouts, and which posts brought inbound.

Both only read saved data (plus the writer models for `replay`). Kept apart from the pipeline so the
flow file holds the flow (eng review R5). This module may import the pipeline; the pipeline must never
import this module.
"""

import json

from . import db, factgate, llm, pipeline
from .config import settings

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
    total: dict[str, float] = {"runs": 0, **dict.fromkeys(keys, 0)}
    print(f"{'run':24} {'pitched':>7} {'failed':>6} {'regen':>5} {'dropped':>7} {'unverif':>7}")
    for path in runs:
        snap = json.loads(path.read_text(encoding="utf-8"))
        cat = factgate.Catalog.from_json(snap["catalog"])
        try:
            batch = pipeline.generate_batch(snap["signals"], snap.get("history", []), pipeline.voice(), cat)
        except Exception as e:
            print(f"{path.stem:24} ERROR {e}")
            continue
        st = (batch or {}).get("stats") or dict.fromkeys(keys, 0)
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
    by: dict[str, dict[str, dict[str, int]]] = {"built": {}, "topic kind": {}}
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
