"""Style examples for the post writer: a private file of public posts that did well.

The writer sees 3 of them per post as examples of structure and rhythm, never of words or facts. The file is
made once with `python -m postpilot examples fetch` (free Apify plan, see sources.py) or written by hand, then
edited freely. It is gitignored: the posts belong to their authors.

File format: posts separated by a line holding only `---`; the first line of a post is its header
(`Author | stats | url`), the rest is the post text with its line breaks.
"""

import re
import zlib
from pathlib import Path
from typing import Any

from . import sources
from .config import settings

MIN_CHARS, MAX_CHARS = 600, 1800        # the length range the eval accepts for a post
KEEP = 30                               # posts kept from a one-time pull
SHOW = 3                                # examples put in one post prompt
SHOW_CHARS = 600                        # per example: the opening; the ending is kept too (TAIL_CHARS)
TAIL_CHARS = 250

# Phrases that mark the posts that won in the first 15 examples (a reviewer's reading of them): something to try today
# for the coding agent people already use. Replace them with your own.
DEFAULT_QUERIES = ["open-sourced its internal AI tool", "Claude Code plugin", "agent skill Codex", "AGENTS.md",
                   "most useful repo for LLM engineers"]

HEADER = """# Style examples

Public posts that did well, kept private (this file is gitignored). The writer sees a few of them as examples of
structure and rhythm and must never copy their words or facts. Edit freely: delete posts you dislike, add your own.
Keep a line with only --- between posts; the first line of a post is its header.
"""


def path() -> Path:
    return Path(settings.examples_file)


# Every post of the first author ended with "--" and a promo P.S.: that is his advert, not his style.
_PROMO = re.compile(r"\n--[ \t]*\n(?:P\.S\.|PS:)[\s\S]*\Z")


def _clean(text: str) -> str:
    """A post as it is stored: no promo P.S., and no line of only --- (that would split it into two posts)."""
    return re.sub(r"(?m)^---[ \t]*$", "- - -", _PROMO.sub("", text.replace("\r\n", "\n")).strip())


def parse(text: str) -> list[dict]:
    """Posts of an examples file. The title block (anything before the first `---`) is not a post."""
    chunks = re.split(r"(?m)^---\s*$", text.replace("\r\n", "\n"))[1:]
    posts = []
    for chunk in chunks:
        lines = chunk.strip("\n").split("\n")
        body = _PROMO.sub("", "\n".join(lines[1:])).strip()
        if len(lines) >= 2 and body:
            posts.append({"header": lines[0].strip(), "text": body})
    return posts


def load(quality: bool = True) -> list[dict]:
    """The posts in your file that teach knowledge, not selling; [] when it is missing or not readable text (an
    editor that saved another encoding). `quality=False` keeps every post, for code that must see the whole file."""
    try:
        posts = parse(path().read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return []
    return [p for p in posts if not quality or not rejected(p["text"])]


_STOP = frozenset(["about", "above", "after", "again", "also", "another", "because", "been", "before", "being", "between", "both", "could", "does", "doing", "done", "down", "during", "each", "even", "every", "from", "further", "have", "having", "here", "into", "just", "like", "made", "make", "many", "more", "most", "much", "must", "only", "other", "over", "same", "should", "since", "some", "such", "than", "that", "their", "them", "then", "there", "these", "they", "this", "those", "through", "under", "until", "upon", "very", "want", "well", "were", "what", "when", "where", "which", "while", "will", "with", "within", "without", "would", "your", "yours", "ours", "post", "posts", "today", "new", "now", "one", "two", "use", "used", "using", "way", "really", "thing", "things"])


def _words(text: str) -> list[str]:
    """The content words of a text (filler words dropped), with repeats."""
    return [w for w in re.findall(r"[a-z0-9+#]{4,}", text.lower()) if w not in _STOP]


# Someone who says "I built / I measured ..." shows their own work; the rest passes along someone else's tool.
OWN_BUILD = re.compile(r"(?i)\b(?:i|we)(?:'ve|’ve| have)?\s+(?:built|measured|tested|ran|run|shipped|wrote|tried|"
                       r"benchmarked|migrated|replaced|cut|moved|fixed|made|trained|deployed|compared|fine-tuned|created|connected|"
                       r"found|removed|set up|added|tracked|caught|evaluated|switched|rewrote|profiled|logged)\b")


def _author(header: str) -> str:
    return header.split("|", 1)[0].strip()


def _likes(header: str) -> int:
    m = re.search(r"([\d,]+) likes", header)
    return int(m.group(1).replace(",", "")) if m else 0


def _shape(text: str) -> tuple[bool, bool]:
    """A rough format: does it use a list, is it long."""
    return (len(re.findall(r"(?m)^\s*(?:[-*•🔹🔸▪✅→]|\d+[.)])", text)) >= 3, len(text) > 1000)


def pick(posts: list[dict], idea: dict, n: int = SHOW) -> list[dict]:
    """Up to n examples by different authors, at most one of them a curator.
    1: the best topic match (filler words dropped, overlap divided by the post's length);
    2: the best first-hand build of a similar format;
    3: the strongest post by likes within its own author's posts.
    Each result is a copy of the post with a 'kind' of 'own-build' or 'curation' (internal: never shown)."""
    about = set(_words(" ".join(str(idea.get(k) or "") for k in ("title", "topic", "handle", "format")) + " "
                        + " ".join(map(str, idea.get("entities") or []))))
    top: dict[str, int] = {}
    for p in posts:
        a = _author(p["header"]).lower()
        top[a] = max(top.get(a, 0), _likes(p["header"]))
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(posts):
        a, w, likes = _author(p["header"]).lower(), _words(p["text"]), _likes(p["header"])
        rows.append({"i": i, "p": p, "author": a, "own": bool(OWN_BUILD.search(p["text"])), "likes": likes,
                     "score": len(about & set(w)) / max(len(w), 1), "rel": likes / top[a] if top[a] else 0.0})
    got: list[dict[str, Any]] = []

    def take(order: list[dict[str, Any]], strict: bool) -> None:
        for r in order:
            if r in got or any(r["author"] == g["author"] for g in got):
                continue
            if strict and not r["own"] and any(not g["own"] for g in got):
                continue                                    # a second curator
            got.append(r)
            return

    seed = str(idea.get("title") or "")
    by_topic = sorted(rows, key=lambda r: (-r["score"], r["i"]))
    if rows and by_topic[0]["score"] == 0:                  # nothing overlaps: the best first-hand build, not file order
        by_topic = sorted(rows, key=lambda r: (not r["own"], -r["likes"], r["i"]))
    # every author's top post has rel 1.0: break ties by topic, then by a hash of the idea (stable per idea)
    by_likes = sorted(rows, key=lambda r: (-round(r["rel"], 2), -r["score"],
                                           zlib.crc32(f"{seed}|{r['i']}".encode()), r["i"]))
    for strict in (True, False):                            # the curator limit gives way only when posts run out
        if not got:
            take(by_topic, strict)
        shape = _shape(got[0]["p"]["text"]) if got else (False, False)
        builds = sorted((r for r in rows if r["own"]),
                        key=lambda r: (_shape(r["p"]["text"]) != shape, -r["score"], r["i"]))
        if 0 < len(got) < 2:
            take(builds, strict)
        if 0 < len(got) < 3:
            take(by_likes, strict)
        while 0 < len(got) < n:
            before = len(got)
            take(by_topic, strict)
            if len(got) == before:
                break
    return [{**r["p"], "kind": "own-build" if r["own"] else "curation"} for r in got[:n]]


def chosen(idea: dict) -> list[dict]:
    """The examples this idea's post is written next to (none without an examples file)."""
    return pick(load(), idea)


def _shown(text: str, head: int) -> str:
    """The opening and the ending of a post: the way it ends is part of its shape."""
    text = re.sub(r"@\w+", "", re.sub(r"(?i)\S*(?:https?://|lnkd\.in)\S*", "[link]", text))
    if len(text) <= head + TAIL_CHARS:
        return text
    return text[:head].rstrip() + "\n[...]\n" + text[-TAIL_CHARS:].lstrip()


def for_prompt(idea: dict, posts: list[dict] | None = None, chars: int = SHOW_CHARS) -> str:
    """Text for the post prompt ('' when there is no examples file): the picked examples, opening and ending.
    The header is 'Example N:' only: no author, likes or links."""
    picked = chosen(idea) if posts is None else posts
    return "\n\n".join(f"Example {i}:\n{_shown(p['text'], chars)}"
                       for i, p in enumerate(picked, 1))


COPY_WORDS = 8          # a draft that repeats this many words in a row from an example has copied it


def shared_run(text: str, posts: list[dict], n: int = COPY_WORDS) -> str:
    """The first run of n or more words a draft shares with one of the examples ('' if none): a copied phrase."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    at = {" ".join(words[i:i + n]): i for i in range(len(words) - n + 1)}      # where each n-word run starts in the draft
    for p in posts:
        w = re.findall(r"[a-z0-9']+", p["text"].lower())
        for i in range(len(w) - n + 1):
            j = at.get(" ".join(w[i:i + n]))
            if j is not None:
                k = n
                while i + k < len(w) and j + k < len(words) and w[i + k] == words[j + k]:
                    k += 1                          # the whole copied run, so a rewrite cannot keep the rest of it
                return " ".join(w[i:i + k])
    return ""


_NUM = re.compile(r"\d[\d,.]*\s?(?:%|x\b|ms|s\b|GB|k\b|M\b|\$)|\$\d[\d,.]*")


def copied_numbers(post: str, picked: list[dict], facts_text: str) -> list[str]:
    """Number-with-unit tokens in the post that come from a picked example, not from the facts."""
    seen = {m.group().strip() for p in picked for m in _NUM.finditer(p["text"])}
    ours = {m.group().strip() for m in _NUM.finditer(facts_text)}
    return sorted(t for t in {m.group().strip() for m in _NUM.finditer(post)} if t in seen and t not in ours)


def _header(row: dict) -> str:
    who = " ".join((row.get("author") or "someone").split())
    return f"{who} | {row['likes']:,} likes, {row['comments']:,} comments | {row.get('url') or 'no link'}"


def render(rows: list[dict]) -> str:
    body = "".join(f"\n---\n{_header(r)}\n{_clean(r['raw'])}\n" for r in rows)
    return HEADER + body + "---\n"


# Posts that sell or recruit teach the wrong thing: they are skipped (you can still add any by hand).
NOT_A_STYLE_EXAMPLE = re.compile(
    r"(?i)(?:we(?:'|’)re hiring|now hiring|#hiring|apply (?:now|here)|job opening|join (?:our|my) team|"
    r"dm me|book a (?:call|demo)|"
    r"repost|follow \w+(?: \w+)? for|star the (?:repo|github)|like and|♻️|"
    r"full (?:episode|video)|the article covers|my (?:course|newsletter|cohort)|subscribe|webinar|"
    r"our agency|we help|our clients|free (?:audit|consultation))")
_CUT_OFF = re.compile(r"(?:https?:/?/?\S{0,3}|[,:])\s*\Z")
_LINKS = re.compile(r"(?i)lnkd\.in|https?://")
_HYPE = re.compile(r"(?i)(?:gold ?mine|🤯|🚨|nobody noticed|stay tuned|game.?changer)")
_LINK_IN_COMMENT = re.compile(r"(?i)(?:link|repo)[^.\n]{0,25}comment")
_MEASURED = re.compile(r"(?i)\d+(?:\.\d+)?\s?(?:%|ms|s\b|x\b|/\d+|GB|tokens)")


def rejected(text: str) -> str:
    """Why a post is not a knowledge example ('' when it is one): selling, hiring, cut off, hype, no measured number."""
    if NOT_A_STYLE_EXAMPLE.search(text):
        return "selling or hiring"
    if _CUT_OFF.search(text):
        return "cut off"
    if len(_LINKS.findall(text)) > 3:
        return "too many links"
    # soft signs: one alone is fine (a link in the comments is normal practice), two together is selling
    soft = [bool(_HYPE.search(text)), len(re.findall(r"#\w+", text)) > 5,
            bool(_LINK_IN_COMMENT.search(text)), not _MEASURED.search(text)]
    if sum(soft) >= 2:
        return "two soft signs: hype, many hashtags, link in comments, no measured number"
    return ""


def best(rows: list[dict], keep: int = KEEP) -> list[dict]:
    """Posts in the length range that teach knowledge (see rejected), most engaged first."""
    fit = [r for r in rows if MIN_CHARS <= r.get("length", len(r["raw"])) <= MAX_CHARS and not rejected(_clean(r["raw"]))]
    return sorted(fit, key=sources._engagement, reverse=True)[:keep]


def _link(header: str) -> str:
    return header.rsplit("|", 1)[-1].strip()


def append(rows: list[dict]) -> int:
    """Add posts to the end of your file, skipping any whose link is already in it. Returns how many were added."""
    target = path()
    old_posts = load(quality=False)
    links = {_link(p["header"]) for p in old_posts}
    starts = {p["text"][:80] for p in old_posts}
    new = [r for r in rows if (r["url"] not in links if r.get("url") else _clean(r["raw"])[:80] not in starts)]
    if new:
        old = target.read_text(encoding="utf-8").rstrip() if target.exists() else HEADER.rstrip()
        old += "\n" if old.endswith("---") else "\n---\n"
        target.write_text(old + "".join(f"{_header(r)}\n{_clean(r['raw'])}\n---\n" for r in new), encoding="utf-8")
    return len(new)


def fetch(queries: list[str] | None = None, authors: list[str] | None = None, force: bool = False, post=None,
          add: bool = False) -> str:
    """One-time pull from the authors you name and/or from searches (the default searches only when you name
    neither). Returns a message for the person. Never overwrites a file you may have edited, unless forced;
    `add` appends the new posts to it instead."""
    if not sources.env("APIFY_TOKEN"):
        return "LinkedIn search is off: set APIFY_TOKEN in .env first (free Apify plan only, see the README)."
    target = path()
    if target.exists() and not force and not add:
        return f"{target} already exists, so nothing was fetched. Use --add to append to it or --force to replace it."
    if not queries and not authors:
        queries = DEFAULT_QUERIES
    rows = sources.linkedin_examples(queries or [], post=post, authors=authors)
    if not rows:
        return "No posts came back (search off, over the monthly cap, or nothing found). Nothing was written."
    keep = best(rows)
    if not keep:
        return f"{len(rows)} posts came back but none was {MIN_CHARS}-{MAX_CHARS} characters long. Nothing was written."
    used, cap = sources.apify_usage()
    if add:
        n = append(keep)
        return (f"Added {n} new posts ({len(keep)} good of {len(rows)} fetched) to {target}. "
                f"Apify posts used this month: {used} of {cap}.")
    target.write_text(render(keep), encoding="utf-8")
    return (f"Saved {len(keep)} of {len(rows)} posts to {target}. Read it and delete what you dislike. "
            f"Apify posts used this month: {used} of {cap}.")


def state() -> str:
    posts = load()
    if posts:
        return f"{len(posts)} posts in {path()}"
    return "none (python -m postpilot examples fetch, or write examples.md by hand)"
