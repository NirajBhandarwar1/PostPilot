"""Signals from real use: what you did with the emails and how the models behaved (ADR-006).

Read-only. No model calls, no network. These are the numbers that say whether the product is
good for you, which the golden-story rules in metrics.py can only guess at:

- pick rate: of the ideas emails old enough to have been answered, how many got a reply
- your ratings: the 1-5 scores you gave the ideas
- model health: share of provider attempts that gave usable output, how often a fallback ran, speed
- results: inbound messages per post
"""
import json
from datetime import UTC, datetime, timedelta

from .. import db

ANSWER_WINDOW_DAYS = 2      # an ideas email younger than this may simply not be answered yet


def _batches(days: int) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, created_at, status, chosen FROM batches WHERE created_at >= ? ORDER BY id", (since,))]


def _pick_rate(days: int) -> dict:
    cutoff = (datetime.now(UTC) - timedelta(days=ANSWER_WINDOW_DAYS)).isoformat()
    old = [b for b in _batches(days) if b["created_at"] <= cutoff and b["status"] != "skipped"]
    picked = [b for b in old if b["chosen"]]
    built = [b for b in old if b["status"] in ("built", "posted")]
    return {"emails": len(old), "picked": len(picked), "built": len(built),
            "pick_rate": round(100 * len(picked) / len(old), 1) if old else None}


def _ratings() -> dict:
    with db.connect() as c:
        rows = c.execute("SELECT value FROM meta WHERE key LIKE 'ratings:%'").fetchall()
    flat = []
    for r in rows:
        try:
            flat += [x for x in json.loads(r["value"]) if isinstance(x, (int, float))]
        except (ValueError, TypeError):
            continue
    return {"rated": len(flat), "average": round(sum(flat) / len(flat), 2) if flat else None}


def _models(days: int) -> dict:
    since = (datetime.now(UTC) - timedelta(days=max(days, 1) - 1)).strftime("%Y-%m-%d")
    with db.connect() as c:
        rows = c.execute(
            "SELECT role, COUNT(*) n, SUM(ok) good, AVG(secs) secs, SUM(tokens) tok FROM llm_calls "
            "WHERE day >= ? AND COALESCE(reason,'') != 'too_big' GROUP BY role", (since,)).fetchall()
    roles = {r["role"]: {"attempts": r["n"], "usable": int(r["good"] or 0),
                         "usable_pct": round(100 * (r["good"] or 0) / r["n"], 1),
                         "avg_seconds": round(r["secs"] or 0, 1), "tokens": int(r["tok"] or 0)} for r in rows}
    n = sum(v["attempts"] for v in roles.values())
    good = sum(v["usable"] for v in roles.values())
    # Every failed attempt means the next provider in the list had to run (or the whole call failed).
    return {"roles": roles, "attempts": n, "fallback_pct": round(100 * (n - good) / n, 1) if n else None}


def _results() -> dict:
    posts = [r for r in db.all_outcomes() if r["posted_url"]]
    inbound = sum((r["recruiters"] or 0) + (r["founders"] or 0) + (r["other"] or 0) for r in posts)
    return {"posts": len(posts), "inbound": inbound,
            "per_post": round(inbound / len(posts), 2) if posts else None}


def signals(days: int = 14) -> dict:
    return {"days": days, "ideas": _pick_rate(days), "ratings": _ratings(), "models": _models(days),
            "results": _results()}


def _pct(v: float | None) -> str:
    return "no data yet" if v is None else f"{v:g}%"


def render(s: dict) -> str:
    i, r, m, res = s["ideas"], s["ratings"], s["models"], s["results"]
    lines = [f"Live signals, last {s['days']} days",
             f"  Pick rate      : {_pct(i['pick_rate'])}  ({i['picked']} of {i['emails']} ideas emails answered, "
             f"{i['built']} built)",
             "  Your ratings   : " + (f"{r['average']}/5 over {r['rated']} ideas" if r["rated"] else "none yet"),
             f"  Fallback rate  : {_pct(m['fallback_pct'])}  ({m['attempts']} model attempts)",
             "  Results        : " + (f"{res['inbound']} inbound from {res['posts']} posts "
                                     f"({res['per_post']} per post)" if res["posts"] else "no posts recorded yet")]
    if m["roles"]:
        lines.append("  Model calls that gave usable output, by task:")
        for role, v in sorted(m["roles"].items()):
            lines.append(f"    {role:8} {v['usable']}/{v['attempts']} ({v['usable_pct']:g}%), "
                         f"{v['avg_seconds']:g}s average, {v['tokens']:,} tokens")
    return "\n".join(lines)
