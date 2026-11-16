"""How hot a story is: measured attention only, plus the bonuses that decide which story is picked.

Pure functions over the collected signals; no model call, no database, no network. Kept apart from the
pipeline so a ranking change does not mean reading the mail and build flow (eng review R5).
"""

import math

MODEL_SCORE_CAP = 15.0   # the model's own opinion (relevance + buzz) can add at most this
# Stories you can get your hands on beat news-only ones (the user's rule: test it, build with it).
HANDLE_BONUS = {"model": 8.0, "github_repo": 8.0, "product": 5.0, "paper": 4.0}


def heat(topic: dict, signals: list[dict], ids: list[int] | None = None) -> tuple[float, str]:
    """Measured attention only: upvotes, curated front pages, trending ranks, capped coverage.
    `ids` should be the sources that really name the story (link_ids)."""
    ids = topic["signal_ids"] if ids is None else ids
    hits = [signals[i] for i in ids if 0 <= i < len(signals)]
    kinds: dict[str, list[dict]] = {}
    for h in hits:
        kinds.setdefault(h["kind"], []).append(h)
    # Real attention beats article counts: upvotes, curated front pages, trending ranks.
    score = 3.0 * len(kinds)                                   # seen in many places = viral
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
