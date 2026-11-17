"""The evaluation runner: golden stories in, scored runs out (ADR-006).

A golden story is a saved public story plus the option and research a past run picked for it
(evals/golden/<name>.json). `run_eval` pushes each one through the CURRENT options and BUILD.md/post
prompts with whatever free providers are configured, scores the outputs with the deterministic
checks in metrics.py, stores the run in evals/results/ and compares it with the previous run, so a
prompt change that makes things worse shows up as a regression instead of a feeling.

Nothing here saves a batch, writes a build folder or sends email. The only database write is the
'eval:last' pointer.
"""

import argparse
import json
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import db, llm, pipeline
from . import metrics
from .metrics import Check

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "evals" / "golden"
RESULTS_DIR = ROOT / "evals" / "results"
LAST_KEY = "eval:last"
MAX_STORIES = 3             # a story costs ~2-3 model calls; the free tiers are small
MAX_DROP = 5.0              # overall points a run may lose before the exit code says so
STAGES = ("options", "plan", "post")
# A provider that says it is empty will not recover during this run: more stories only burn time.
USED_UP = re.compile(r"no credit|used up|daily cap|quota", re.I)
SECRETS = (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                       # email addresses
           re.compile(r"\b(?:sk|gsk|hf|ghp|xox[bp])[-_][A-Za-z0-9_-]{16,}"))   # API-key shapes


# ---------- golden set ----------

def _scrub(value: Any) -> Any:
    """Golden files are committed: drop anything shaped like an email address or an API key."""
    if isinstance(value, str):
        for pat in SECRETS:
            value = pat.sub("[removed]", value)
        return value
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def capture(batch_id: int, idea_no: int, option_no: int, name: str) -> Path:
    """Save idea `idea_no` / option `option_no` of a saved batch as evals/golden/<name>.json.
    Only the story's public text goes in: no voice profile, no settings, no batch metadata."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError("name must be lowercase letters, digits and dashes, like 'agent-skills-cli'")
    row = db.get_batch(batch_id)
    if not row:
        raise ValueError(f"No batch #{batch_id}")
    data = json.loads(row["ideas_json"])
    ideas = data.get("ideas", [])
    if not 1 <= idea_no <= len(ideas):
        raise ValueError(f"Batch #{batch_id} has ideas 1-{len(ideas)}")
    listing = (data.get("options") or {}).get(str(idea_no))
    if not listing:
        raise ValueError(f"Idea {idea_no} of batch #{batch_id} has no saved options "
                         f"(run: python -m postpilot options {batch_id} {idea_no})")
    opts = listing.get("options", [])
    if not 1 <= option_no <= len(opts):
        raise ValueError(f"Idea {idea_no} of batch #{batch_id} has options 1-{len(opts)}")
    story = _scrub({"name": name, "captured_at": db.now_iso(), "idea": ideas[idea_no - 1],
                    "option": opts[option_no - 1], "research": listing.get("research"),
                    "evidence_pack": listing.get("evidence_pack")})
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    path.write_text(json.dumps(story, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def golden_stories(names: list[str] | None = None) -> list[dict]:
    """The golden stories in name order, or just `names` (an unknown name is an error)."""
    found = {p.stem: p for p in sorted(GOLDEN_DIR.glob("*.json"))}
    wanted = names or list(found)
    missing = [n for n in wanted if n not in found]
    if missing:
        raise ValueError(f"No golden story {', '.join(missing)}. Have: {', '.join(found) or 'none'}")
    return [json.loads(found[n].read_text(encoding="utf-8")) for n in wanted]


# ---------- scoring one story ----------

def _score(outputs: dict, errors: dict) -> tuple[list[Check], dict]:
    """Checks + summary for one story's produced outputs. Used by the live run and by replay, so a
    re-scored run and a fresh one are judged by exactly the same code."""
    checks: list[Check] = []
    # A stage that crashed must cost points: otherwise a broken prompt would score higher than a weak one.
    for stage in ("options", "plan"):
        if stage in outputs or stage in errors:      # a stage that was never started is not scored at all
            checks.append(Check(f"{stage} stage ran", stage not in errors, errors.get(stage, ""), 1.0, stage))
    if "options" in outputs:
        checks += metrics.evaluate_options(outputs["options"], outputs.get("dropped", []))
    if "plan" in outputs:
        checks += metrics.evaluate_plan(outputs["plan"])
        checks += metrics.evaluate_post(outputs["plan"])
    return checks, metrics.summarize(checks)


def _plain(plan: dict) -> dict:
    """The plan without its pydantic objects, so it can be written to JSON and scored again later."""
    out = {k: v for k, v in plan.items() if k not in ("brief", "kit")}
    for k in ("brief", "kit"):
        if hasattr(plan.get(k), "model_dump"):
            out[k] = plan[k].model_dump()
    return out


def _merge_usage(total: dict, snap: dict) -> None:
    for model, u in snap.items():
        t = total.setdefault(model, {"calls": 0, "in": 0, "out": 0})
        for k in t:
            t[k] += u.get(k, 0)


def _run_story(story: dict) -> dict:
    """Run one golden story through the current pipeline. Stage failures are recorded, not raised."""
    idea = story["idea"]
    outputs: dict = {}
    errors: dict = {}
    stages: dict = {}
    usage: dict = {}
    for stage in ("options", "plan"):
        t0 = time.time()
        try:
            if stage == "options":
                res, dropped = pipeline.make_options(idea)
                outputs["options"], outputs["dropped"] = res, dropped
            else:
                picked = pipeline.idea_from_option(idea, story["option"], story.get("research"),
                                                    story.get("evidence_pack"))
                outputs["plan"] = _plain(pipeline.make_plan(picked))
        except Exception as e:      # one failing stage must not hide the other one's result
            errors[stage] = f"{type(e).__name__}: {str(e)[:300]}"
        # Each stage resets llm.USAGE, so read it right after the stage.
        _merge_usage(usage, {m: dict(u) for m, u in llm.USAGE.items()})
        stages[stage] = {"seconds": round(time.time() - t0, 1), "error": errors.get(stage, "")}
        if stage in errors and USED_UP.search(errors[stage]):
            break               # no point asking the next stage of an empty provider
    checks, summary = _score(outputs, errors)
    # A provider that is out of quota stops the run before every stage was tried. The score would show the
    # quota, not the prompts, so such a story is marked and kept out of comparisons.
    return {"title": idea.get("title", ""), "incomplete": any(USED_UP.search(e) for e in errors.values()),
            "stages": stages, "seconds": round(sum(s["seconds"] for s in stages.values()), 1),
            "tokens": {"in": sum(u["in"] for u in usage.values()), "out": sum(u["out"] for u in usage.values()),
                       "calls": sum(u["calls"] for u in usage.values())},
            "providers": sorted(usage), "errors": errors, "outputs": outputs,
            "checks": [asdict(c) for c in checks], "summary": summary}


def _overall(stories: dict) -> float:
    scores = [s["summary"]["score"] for s in stories.values()]
    return round(sum(scores) / len(scores), 1) if scores else 0.0


# ---------- run / store / compare ----------

def run_eval(names: list[str] | None = None, stories: list[dict] | None = None,
             max_stories: int = MAX_STORIES) -> dict:
    """Score the current pipeline on the golden stories, one at a time (free tiers are small).
    Returns the run dict (not stored yet: see save_run)."""
    chosen = stories if stories is not None else golden_stories(names)
    chosen = chosen[:max_stories]
    run: dict = {"started_at": datetime.now(UTC).isoformat(timespec="seconds"), "stories": {},
                 "skipped": []}
    for i, story in enumerate(chosen):
        name = story.get("name", f"story-{i + 1}")
        print(f"running {name} ({i + 1}/{len(chosen)}) ...", flush=True)
        result = _run_story(story)
        run["stories"][name] = result
        blown = [e for e in result["errors"].values() if USED_UP.search(e)]
        if blown and i + 1 < len(chosen):
            run["skipped"] = [s.get("name", "?") for s in chosen[i + 1:]]
            print(f"A provider reports it is used up ({blown[0][:120]}). Stopping early; skipped: "
                  f"{', '.join(run['skipped'])}. Try again after the quota resets.")
            break
    run["overall"] = _overall(run["stories"])
    return run


def save_run(run: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / (datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ") + ".json")
    path.write_text(json.dumps(run, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    db.set_meta(LAST_KEY, path.name)
    return path


def load_run(path_or_name: str | Path | None = None) -> dict | None:
    """A stored run by path, by file name/stem, or (None) the last one. None when there is no such run."""
    if path_or_name is None:
        path_or_name = db.get_meta(LAST_KEY) or ""
        if not path_or_name:
            return None
    p = Path(path_or_name)
    if not p.exists():
        p = RESULTS_DIR / (p.name if p.suffix == ".json" else p.name + ".json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def compare(prev: dict | None, cur: dict) -> dict:
    """Deltas between two runs over the stories both contain: per stage, overall, and per check.
    A check that passed before and fails now is a regression; the reverse is a fix."""
    out: dict = {"overall": None, "stages": {}, "regressions": [], "fixed": []}
    if not prev:
        return out
    common = [n for n in cur["stories"] if n in prev["stories"]]
    if not common:
        return out
    for stage in STAGES:
        a = [prev["stories"][n]["summary"]["by_stage"][stage] for n in common
             if stage in prev["stories"][n]["summary"]["by_stage"]]
        b = [cur["stories"][n]["summary"]["by_stage"][stage] for n in common
             if stage in cur["stories"][n]["summary"]["by_stage"]]
        if a and b:
            out["stages"][stage] = round(sum(b) / len(b) - sum(a) / len(a), 1)
    old = sum(prev["stories"][n]["summary"]["score"] for n in common) / len(common)
    new = sum(cur["stories"][n]["summary"]["score"] for n in common) / len(common)
    out["overall"] = round(new - old, 1)
    for n in common:
        was = {c["name"]: c for c in prev["stories"][n]["checks"]}
        for c in cur["stories"][n]["checks"]:
            before = was.get(c["name"])
            if before is None:
                continue
            item = {"story": n, "check": c["name"], "weight": c["weight"], "detail": c["detail"]}
            if before["passed"] and not c["passed"]:
                out["regressions"].append(item)
            elif c["passed"] and not before["passed"]:
                out["fixed"].append(item)
    return out


def exit_code(diff: dict, max_drop: float = MAX_DROP) -> int:
    """1 when the run got clearly worse: the overall score fell by more than max_drop points, or a
    check that mattered (weight >= 1) stopped passing on any story."""
    if diff["overall"] is not None and diff["overall"] < -max_drop:
        return 1
    return 1 if any(r["weight"] >= 1 for r in diff["regressions"]) else 0


# ---------- output ----------

def _short(models: list[str]) -> str:
    return ", ".join(sorted({m.split(":")[0] + ":" + m.rsplit("/", 1)[-1][:18] for m in models})) or "-"


def render(run: dict, diff: dict | None = None) -> str:
    rows = ["| story | options | plan | post | score | sec | tokens | providers |",
            "|---|---|---|---|---|---|---|---|"]
    for name, s in run["stories"].items():
        by = s["summary"]["by_stage"]
        cells = [f"{by[st]:.0f}" if st in by else "-" for st in STAGES]
        tok = s["tokens"]["in"] + s["tokens"]["out"]
        rows.append(f"| {name} | {' | '.join(cells)} | {s['summary']['score']:.0f} | {s['seconds']:.0f} | "
                    f"{tok:,} | {_short(s['providers'])} |")
    secs = sum(s["seconds"] for s in run["stories"].values())
    tok = sum(s["tokens"]["in"] + s["tokens"]["out"] for s in run["stories"].values())
    rows.append(f"| **overall** | | | | **{run['overall']:.1f}** | {secs:.0f} | {tok:,} | |")
    lines = rows
    for name, s in run["stories"].items():
        for stage, err in s["errors"].items():
            lines.append(f"\n{name}: {stage} stage failed: {err}")
    if diff is not None:
        if diff["overall"] is None:
            lines.append("\nNo previous run to compare with.")
        else:
            by_stage = ", ".join(f"{k} {v:+.1f}" for k, v in diff["stages"].items())
            lines.append(f"\nvs previous run: overall {diff['overall']:+.1f} ({by_stage})")
        for title, key in (("Regressions", "regressions"), ("Fixed", "fixed")):
            items = diff[key]
            lines.append(f"\n{title}:" + ("" if items else " none"))
            lines += [f"- {i['story']}: {i['check']}" + (f" ({i['detail']})" if i["detail"] else "") for i in items]
    return "\n".join(lines)


# ---------- offline replay ----------

def replay(path_or_name: str | Path | None = None) -> dict:
    """Score a saved run's stored outputs again with the CURRENT metrics: zero model calls, so a
    changed metric can be tried on real outputs for free. Prints the change against the stored scores."""
    old = load_run(path_or_name)
    if old is None:
        raise ValueError("No saved run to replay. Run `python -m postpilot eval run` first.")
    new = {"started_at": old.get("started_at", ""), "skipped": [], "stories": {}}
    for name, s in old["stories"].items():
        checks, summary = _score(s.get("outputs", {}), s.get("errors", {}))
        new["stories"][name] = {**s, "checks": [asdict(c) for c in checks], "summary": summary}
    new["overall"] = _overall(new["stories"])
    print(render(new, compare(old, new)))
    return new


# ---------- command line ----------

def list_stories() -> int:
    last = load_run()
    for p in sorted(GOLDEN_DIR.glob("*.json")):
        s = last["stories"].get(p.stem) if last else None
        score = f"last score {s['summary']['score']:.0f}" if s else "not run yet"
        print(f"{p.stem:28} {score}")
    if not list(GOLDEN_DIR.glob("*.json")):
        print("No golden stories. Add one: python -m postpilot eval capture BATCH IDEA OPTION NAME")
    return 0


def cli(args: argparse.Namespace) -> int:
    """`postpilot eval ...`; returns the process exit code."""
    try:
        if args.eval_cmd == "capture":
            print(f"Saved {capture(args.batch, args.idea, args.option, args.name)}")
        elif args.eval_cmd == "replay":
            replay(args.run_file)
        elif args.eval_cmd == "list":
            return list_stories()
        else:
            for line in llm.quota_lines():              # the daily scout uses the same free quota
                print(f"Free quota today: {line}")
            prev = load_run()                           # before saving: the new run becomes 'last'
            run = run_eval(names=args.story or None, max_stories=args.max)
            if not run["stories"]:
                print("No golden stories to run. Add one with `eval capture`.")
                return 1
            if any(s["incomplete"] for s in run["stories"].values()):
                print("\n" + render(run) + "\n\nIncomplete: a provider ran out of quota, so this run is not saved "
                      "and not compared. Try again after the quota resets (`status` shows it).")
                return 0 if args.no_fail else 3
            diff = compare(prev, run)
            path = save_run(run)
            print("\n" + render(run, diff) + f"\n\nSaved {path}")
            return 0 if args.no_fail else exit_code(diff)
    except ValueError as e:
        print(str(e))
        return 2
    return 0
