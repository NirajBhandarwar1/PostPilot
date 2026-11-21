"""Pure, deterministic scoring of PostPilot output. No model calls, no network, no database.

Every metric is one small named Check so a failing eval says exactly what is wrong. The functions
never raise: a missing section or a malformed value is a failed check with a clear detail, because
an eval that crashes on bad output hides the very thing it exists to catch.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# The eval's own bars. They may be stricter than the app on purpose (the eval wants 3+ options, the pipeline
# accepts 2). Only SCORE_GOOD must equal pipeline.POST_TARGET; tests/test_eval_metrics.py checks that one.
MIN_OPTIONS = 3
MAX_DROPPED = 1
MIN_EFFORT, MAX_EFFORT = 20, 240
MIN_STEPS, MAX_STEPS = 4, 10
MAX_HUMAN_SHARE = 0.60
TIME_SLACK = 1.6                    # the pipeline warns at 1.5x; the eval fails a little later, on purpose
MAX_DOC_CHARS = 25_000
POST_MIN, POST_MAX = 600, 1800
HOOK_MAX = 140
MAX_HASHTAGS = 3                    # same as the post prompt in brain.py
SCORE_PASS, SCORE_GOOD = 70, 75
# Bump when a check changes what it accepts: runs scored by different versions are not comparable overall.
METRICS_VERSION = 2

# A warning that tells the person to look, not that the plan is wrong. Anything else counts as a defect.
INFORMATIONAL_WARNINGS = ("need to check", "no step uses", "unverified claim")


@dataclass(frozen=True)
class Check:
    name: str          # short snake_case id, e.g. "options_has_demo_and_repo"
    passed: bool
    detail: str = ""   # one short human sentence, why it failed (or the number)
    weight: float = 1.0
    stage: str = ""    # "options" | "plan" | "post"


def _guarded(stage: str, fns: Iterable[Callable[[], Check]]) -> list[Check]:
    """Run each check; one that blows up becomes a failed check instead of aborting the whole eval."""
    out = []
    for fn in fns:
        try:
            out.append(fn())
        except Exception as e:
            out.append(Check(fn.__name__.removeprefix("_c_"), False, f"check crashed: {type(e).__name__}", 1.0, stage))
    return out


# ---------- options ----------

def _opts(res: dict) -> list[dict]:
    v = res.get("options") if isinstance(res, dict) else None
    return [o for o in v if isinstance(o, dict)] if isinstance(v, list) else []


def evaluate_options(res: dict, dropped: list[str]) -> list[Check]:
    from .. import pipeline  # late import: pipeline pulls in the model clients

    opts = _opts(res)
    S = "options"

    def _c_options_min_count() -> Check:
        return Check("options_min_count", len(opts) >= MIN_OPTIONS, f"{len(opts)} option(s) kept, need {MIN_OPTIONS}+",
                     1.0, S)

    def _c_options_has_demo_and_repo() -> Check:
        missing = [k for k in pipeline.REQUIRED_KINDS if not any(o.get("kind") == k for o in opts)]
        return Check("options_has_demo_and_repo", not missing, f"missing kind: {', '.join(missing)}" if missing
                     else "all required kinds present", 1.5, S)

    def _c_options_complete_fields() -> Check:
        bad = []
        for n, o in enumerate(opts, 1):
            steps = o.get("steps_preview") or o.get("steps")
            gaps = [k for k, v in (("what_you_build", o.get("what_you_build")), ("steps", steps),
                                   ("hook", o.get("hook")), ("minutes", o.get("minutes"))) if not v]
            if gaps:
                bad.append(f"option {n} lacks {'/'.join(gaps)}")
        if not opts:
            bad.append("no options")
        return Check("options_complete_fields", not bad, "; ".join(bad[:3]), 1.0, S)

    def _c_options_recommended_valid() -> Check:
        rec = res.get("recommended") if isinstance(res, dict) else None
        ok = isinstance(rec, int) and not isinstance(rec, bool) and 1 <= rec <= len(opts)
        return Check("options_recommended_valid", ok, f"recommended={rec!r} with {len(opts)} option(s)", 1.0, S)

    def _c_options_few_dropped() -> Check:
        n = len(dropped or [])
        return Check("options_few_dropped", n <= MAX_DROPPED, f"{n} option(s) dropped by the fact check", 1.0, S)

    def _c_options_no_model_warnings() -> Check:
        hits = [o.get("title", "?") for o in opts
                if any("need to check" in str(w).lower() for w in (o.get("check_before_posting") or []))]
        # Informational: a model that is down today is not a writing defect, so it counts half.
        return Check("options_no_model_warnings", not hits, f"model unconfirmed in: {', '.join(map(str, hits[:3]))}",
                     0.5, S)

    def _c_options_effort_in_range() -> Check:
        bad = []
        for n, o in enumerate(opts, 1):
            m = o.get("minutes")
            if not isinstance(m, (int, float)) or isinstance(m, bool) or not MIN_EFFORT <= m <= MAX_EFFORT:
                bad.append(f"option {n}: {m!r} min")
        if not opts:
            bad.append("no options")
        detail = f"outside {MIN_EFFORT}-{MAX_EFFORT} min: " + "; ".join(bad[:3]) if bad else ""
        return Check("options_effort_in_range", not bad, detail, 1.0, S)

    def _c_options_name_a_source() -> Check:
        bad = [str(n) for n, o in enumerate(opts, 1) if not str(o.get("inspired_by") or "").strip()]
        if not opts:
            bad = ["-"]
        return Check("options_name_a_source", not bad, f"no inspired_by on option(s) {', '.join(bad)}", 1.0, S)

    def _field(o: dict, k: str) -> str:
        v = o.get(k)
        return " ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v or "")

    def _c_options_have_baseline() -> Check:
        pat = re.compile(r"(?i)baseline|control (?:group|run)|\bvs\b|versus|without (it|the)|no-model|rules-only|"
                         r"plain (?:prompt|model|regex|script)|cheapest")
        bad = []
        for n, o in enumerate(opts, 1):
            if o.get("kind") == "demo-app" or str(o.get("baseline") or "").strip():
                continue
            if not pat.search(" ".join(_field(o, k) for k in ("what_you_build", "steps_preview", "your_edge"))):
                bad.append(str(n))
        return Check("options_have_baseline", not bad, f"no baseline in option(s) {', '.join(bad)}", 1.0, S)

    def _c_options_sized_and_scored() -> Check:
        from .. import factgate
        metric = re.compile(r"(?i)\bpass(?:es|ed)?\b|accuracy|\bF1\b|latency|\bcost\b|\$|\bms\b|solved")
        bad = []
        for n, o in enumerate(opts, 1):
            if o.get("kind") == "demo-app":
                continue
            txt = " ".join(_field(o, k) for k in ("what_you_build", "steps_preview", "your_edge", "pass_rule", "runs",
                                                  "limit", "title", "hook"))
            sized = re.sub(r"(?i)\b\d+\s*(?:minutes?|mins?|hours?|hrs?)\b", " ", txt)   # effort is not a sample size
            if not factgate.SETUP_COUNT.search(sized.lower()) or not metric.search(txt):
                bad.append(str(n))
        return Check("options_sized_and_scored", not bad, f"no size or metric in option(s) {', '.join(bad)}", 1.0, S)

    return _guarded(S, [_c_options_have_baseline, _c_options_sized_and_scored, _c_options_min_count, _c_options_has_demo_and_repo, _c_options_complete_fields,
                        _c_options_recommended_valid, _c_options_few_dropped, _c_options_no_model_warnings,
                        _c_options_effort_in_range, _c_options_name_a_source])


# ---------- BUILD.md parsing ----------

_STEP = re.compile(r"^###\s+Step\s+(\d+)\s*([^:\n]*?):\s*(.*?)(?:\s+\(~(\d+)\s*min\))?\s*$", re.M)
_HOURS = re.compile(r"Realistic time:\s*~?\s*([\d.]+)\s*(h|hr|hours?|min\w*)", re.I)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_BAD_HYPHENS = "‐‑‒–—―−"


@dataclass(frozen=True)
class _Step:
    n: int
    who: str          # "human" | "cursor"
    title: str
    minutes: int
    body: str


def _doc(plan: dict) -> str:
    d = plan.get("doc") if isinstance(plan, dict) else ""
    return d if isinstance(d, str) else ""


def _part_a(doc: str) -> str:
    cut = doc.find("# PART B")
    return doc if cut < 0 else doc[:cut]


def _steps(doc: str) -> list[_Step]:
    """The '### Step N' blocks of Part A. A block ends at the next step or the next '## ' heading."""
    a = _part_a(doc)
    ms = list(_STEP.finditer(a))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(a)
        nxt = re.search(r"^## ", a[m.end():end], re.M)
        if nxt:
            end = m.end() + nxt.start()
        out.append(_Step(int(m.group(1)), "human" if "\U0001F9D1" in m.group(2) else "cursor",
                         m.group(3), int(m.group(4) or 0), a[m.end():end]))
    return out


def _promised_minutes(doc: str) -> float | None:
    m = _HOURS.search(doc)
    if not m:
        return None
    v = float(m.group(1))
    return v * 60 if m.group(2).lower().startswith("h") else v


def _code_and_spans(text: str) -> list[str]:
    """Lines that are commands: everything inside code fences plus inline `code` spans."""
    out: list[str] = []
    in_code = False
    for ln in text.splitlines():
        m = _FENCE.match(ln)
        if m and ((in_code and not m.group(2).strip()) or not in_code):
            in_code = not in_code
            continue
        out.extend([ln] if in_code else re.findall(r"`([^`\n]+)`", ln))
    return out


def _fences_balanced(doc: str) -> bool:
    open_run = ""
    for ln in doc.splitlines():
        m = _FENCE.match(ln)
        if not m:
            continue
        run, rest = m.group(1), m.group(2).strip()
        if not open_run:
            open_run = run
        elif not rest and run[0] == open_run[0] and len(run) >= len(open_run):
            open_run = ""
    return not open_run


def _usage_problems(doc: str, marker: str) -> list[str]:
    from .. import factgate
    msgs: list[str] = []
    for ln in _part_a(doc).splitlines():
        msgs.extend(w for w in factgate._wrong_usage(ln) if marker in w)
    return msgs


def evaluate_plan(plan: dict) -> list[Check]:
    doc = _doc(plan)
    steps = _steps(doc)
    S = "plan"

    def _c_plan_has_parts() -> Check:
        miss = [p for p in ("# PART A", "# PART B") if p not in doc]
        return Check("plan_has_parts", not miss, f"missing {', '.join(miss)}" if miss else "", 1.5, S)

    def _c_plan_step_count() -> Check:
        return Check("plan_step_count", MIN_STEPS <= len(steps) <= MAX_STEPS,
                     f"{len(steps)} steps, want {MIN_STEPS}-{MAX_STEPS}", 1.0, S)

    def _c_plan_human_share() -> Check:
        if not steps:
            return Check("plan_human_share", False, "no steps found", 1.0, S)
        share = sum(s.who == "human" for s in steps) / len(steps)
        return Check("plan_human_share", share <= MAX_HUMAN_SHARE, f"{share:.0%} of steps are for the human, "
                     f"max {MAX_HUMAN_SHARE:.0%}", 1.0, S)

    def _c_plan_time_adds_up() -> Check:
        promised = _promised_minutes(doc)
        total = sum(s.minutes for s in steps)
        if promised is None or not steps:
            return Check("plan_time_adds_up", False, "no 'Realistic time' or no steps", 1.0, S)
        return Check("plan_time_adds_up", total <= TIME_SLACK * promised,
                     f"steps add up to {total} min, promised {promised:.0f} min", 1.0, S)

    def _c_plan_steps_have_done_when() -> Check:
        bad = [str(s.n) for s in steps if "done when" not in s.body.lower()]
        if not steps:
            bad = ["-"]
        return Check("plan_steps_have_done_when", not bad, f"no 'Done when' in step(s) {', '.join(bad)}" if bad else "",
                     1.5, S)

    def _c_plan_no_old_openai_call() -> Check:
        p = _usage_problems(doc, "ChatCompletion")
        return Check("plan_no_old_openai_call", not p, "uses removed ChatCompletion API" if p else "", 1.0, S)

    def _c_plan_no_prefixed_model_ids() -> Check:
        p = _usage_problems(doc, "provider prefix")
        return Check("plan_no_prefixed_model_ids", not p, p[0][:90] if p else "", 1.0, S)

    def _c_plan_no_npm_in_pip() -> Check:
        p = _usage_problems(doc, "JavaScript package")
        return Check("plan_no_npm_in_pip", not p, p[0][:90] if p else "", 1.0, S)

    def _c_plan_ascii_commands() -> Check:
        bad = [ln for s in steps for ln in _code_and_spans(s.body) if any(c in ln for c in _BAD_HYPHENS)]
        return Check("plan_ascii_commands", not bad, f"non-ASCII hyphen in command: {bad[0][:50]}" if bad else "",
                     1.0, S)

    def _c_plan_fences_clean() -> Check:
        if "'''" in doc:
            return Check("plan_fences_clean", False, "found ''' where a ``` fence belongs", 1.0, S)
        if "```" not in doc:
            return Check("plan_fences_clean", False, "no ``` code fences at all", 1.0, S)
        ok = _fences_balanced(doc)
        return Check("plan_fences_clean", ok, "" if ok else "a code fence is never closed", 1.0, S)

    def _c_plan_no_duplicate_steps() -> Check:
        rec = [s.n for s in steps if re.search(r"\brecord", s.title, re.I)]
        pub = [s.n for s in steps if re.search(r"\bpublish", s.title, re.I)]
        dup = []
        if len(rec) > 1:
            dup.append(f"record steps {rec}")
        if len(pub) > 1:
            dup.append(f"publish steps {pub}")
        return Check("plan_no_duplicate_steps", not dup, "duplicate " + ", ".join(dup) if dup else "", 1.0, S)

    def _c_plan_warnings_clean() -> Check:
        ws = [str(w) for w in (plan.get("warnings") or [])] if isinstance(plan, dict) else []
        real = [w for w in ws if not w.lower().startswith(INFORMATIONAL_WARNINGS)]
        return Check("plan_warnings_clean", not real, real[0][:100] if real else "", 1.0, S)

    def _c_plan_size_ok() -> Check:
        return Check("plan_size_ok", 0 < len(doc) <= MAX_DOC_CHARS, f"{len(doc)} chars, max {MAX_DOC_CHARS}", 1.0, S)

    def _c_plan_third_party_gated() -> Check:
        from .. import render
        # Step 1 is the fixed template (venv + pytest), which PostPilot itself vouches for.
        bad = [str(s.n) for s in steps if s.n > 1 and s.who == "cursor" and "\U0001F9D1⚠" not in s.body
               and render._runs_third_party(s.title + "\n" + s.body)]
        return Check("plan_third_party_gated", not bad, f"step(s) {', '.join(bad)} fetch/run third-party code "
                     "but are not marked for the human" if bad else "", 2.0, S)

    def _c_plan_pass_rule_first() -> Check:
        rule = re.compile(r"(?i)(?:pass rule|success criteria)\**\s*:")
        runs = re.compile(r"(?i)\b(?:run|runs|running|score|scoring|evaluate|benchmark|measure)\b")
        r = next((i for i, st in enumerate(steps) if rule.search(st.title + "\n" + st.body)), None)
        x = next((i for i, st in enumerate(steps) if st.n > 1 and runs.search(st.title)), None)
        ok = r is not None and (x is None or r < x)
        return Check("plan_pass_rule_first", ok, "no step states the pass rule" if r is None
                     else "the pass rule comes after the step that runs the experiment" if not ok else "", 1.0, S)

    return _guarded(S, [_c_plan_pass_rule_first, _c_plan_has_parts, _c_plan_step_count, _c_plan_human_share, _c_plan_time_adds_up,
                        _c_plan_steps_have_done_when, _c_plan_no_old_openai_call, _c_plan_no_prefixed_model_ids,
                        _c_plan_no_npm_in_pip, _c_plan_ascii_commands, _c_plan_fences_clean,
                        _c_plan_no_duplicate_steps, _c_plan_warnings_clean, _c_plan_size_ok,
                        _c_plan_third_party_gated])


# ---------- post ----------

def _post_text(doc: str) -> str | None:
    """The fenced post under '## 2. The post'. None when the section or its fence is missing."""
    m = re.search(r"^##\s+2\.\s*The post[^\n]*\n", doc, re.M)
    if not m:
        return None
    lines, out, in_code, run = doc[m.end():].splitlines(), [], False, ""  # type: ignore[var-annotated]
    for ln in lines:
        f = _FENCE.match(ln)
        if f and not in_code:
            in_code, run = True, f.group(1)
            continue
        if f and in_code and not f.group(2).strip() and len(f.group(1)) >= len(run):
            return "\n".join(out).strip()
        if in_code:
            out.append(ln)
        elif ln.startswith("## "):
            break
    return None


_HASHTAG = re.compile(r"(?<![\w&])#[A-Za-z]\w*")
_DISCUSS = re.compile(r"\?|\b(?:let me know|tell me|curious|thoughts|what do you think|drop a comment|reply)\b", re.I)


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_HAS_N = re.compile(r"\d+\s*/\s*\d+|\bof\s+\d+|\bout of\s+\d+", re.I)
_LIMIT = re.compile(r"(?i)\blimits?\s*:|limitation|caveat|one run|single run|one seed|synthetic|"
                    r"small (?:test|sample)|not a benchmark|haven't proven|early result")
_GENERIC_END = re.compile(r"(?i)^(?:any |your )?(?:thoughts|what do you think|what are your thoughts|do you agree|agree)\W*$")
_TAKEAWAY = re.compile(r"(?i)\b(?:rule of thumb|takeaway|lesson|next time|before you|if you)\b")
_PCT_X = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|x\b)")
_OPTION_FIELDS = ("title", "what_you_build", "baseline", "pass_rule", "runs", "limit")


def _norm_num(x: str) -> str:
    return x.replace(",", "").rstrip(".")


def evaluate_post(plan: dict) -> list[Check]:
    doc = _doc(plan)
    post = _post_text(doc)
    score = plan.get("score") if isinstance(plan, dict) else None
    S = "post"
    body = post or ""
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    bare = re.sub(r"\{\{.*?\}\}", " ", body)

    def _need_post(name: str, weight: float = 1.0) -> Check | None:
        return None if post else Check(name, False, "the post section is missing", weight, S)

    def _num() -> float | None:
        return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None

    def _c_post_human_score() -> Check:
        v = _num()
        return Check("post_human_score", v is not None and v >= SCORE_PASS,
                     f"human score {v}, need {SCORE_PASS}+" if v is not None else "no human score", 2.0, S)

    def _c_post_human_score_good() -> Check:
        v = _num()
        # 70 passes the scorer, 75 is the target the pipeline rewrites toward (POST_TARGET).
        return Check("post_human_score_good", v is not None and v >= SCORE_GOOD,
                     f"human score {v}, target {SCORE_GOOD}+" if v is not None else "no human score", 1.0, S)

    def _c_post_hook_short() -> Check:
        return _need_post("post_hook_short") or Check("post_hook_short", 0 < len(first) <= HOOK_MAX,
                                                      f"first line is {len(first)} chars, max {HOOK_MAX}", 1.0, S)

    def _c_post_length_ok() -> Check:
        return _need_post("post_length_ok") or Check("post_length_ok", POST_MIN <= len(body) <= POST_MAX,
                                                     f"{len(body)} chars, want {POST_MIN}-{POST_MAX}", 1.0, S)

    def _c_post_no_invented_claims() -> Check:
        from .. import factgate
        if (c := _need_post("post_no_invented_claims", 2.0)):
            return c
        hits = factgate.invented_claims([body], [])
        return Check("post_no_invented_claims", not hits, hits[0][:110] if hits else "", 2.0, S)

    def _c_post_uses_placeholders() -> Check:
        n = len(re.findall(r"\{\{[^}]+\}\}", body))
        # Informational (weight 0): an unfinished post must not earn points for its blanks.
        return _need_post("post_uses_placeholders", 0.0) or Check("post_uses_placeholders", True,
                                                                  f"{n} placeholder(s)", 0.0, S)

    def _c_post_numbers_traceable() -> Check:
        from .. import factgate
        if (c := _need_post("post_numbers_traceable")):
            return c
        low = _HASHTAG.sub(" ", bare).lower()
        low = re.sub(r"https?://\S+", " ", low)
        ents = plan.get("entities") if isinstance(plan, dict) else None
        for nm in sorted({str(e).lower() for e in (ents or []) if e}, key=len, reverse=True):
            low = low.replace(nm, " ")
        low = re.sub(r"\b[a-z]+[\w.-]*\d[\w.-]*\b", " ", low)
        low = factgate.SETUP_COUNT.sub(" ", low)
        # Only the evidence and the option's own fields count: the BUILD.md is full of step numbers and minutes.
        pack = str((plan.get("evidence_pack") if isinstance(plan, dict) else "") or "")
        opt = plan.get("option") if isinstance(plan, dict) else None
        opt_txt = " ".join(str(opt.get(k) or "") for k in _OPTION_FIELDS) if isinstance(opt, dict) else ""
        have = {_norm_num(x) for x in _NUM.findall(pack + " " + opt_txt)}
        ev_have = {_norm_num(x) for x in _NUM.findall(pack)}      # a % or Nx figure must come from the evidence
        miss = [x for x in _PCT_X.findall(low) if _norm_num(_NUM.findall(x)[0]) not in ev_have]
        miss += [x for x in _NUM.findall(low) if _norm_num(x) not in have]
        return Check("post_numbers_traceable", not miss,
                     f"number not in the evidence or plan: {miss[0]}" if miss else "", 1.0, S)

    def _c_post_ends_with_question() -> Check:
        # Name kept for old result files. Passes on a question, a takeaway/rule, or a single link; fails on filler.
        if (c := _need_post("post_ends_with_question")):
            return c
        paras = [p for p in re.split(r"\n\s*\n", body) if _HASHTAG.sub("", p).strip()]
        last = _HASHTAG.sub("", paras[-1]).strip() if paras else ""
        tail = re.split(r"(?<=[.!?])\s+|\n", last)[-1].strip()
        if _GENERIC_END.match(tail):
            return Check("post_ends_with_question", False, f"generic ending: {tail[:40]}", 1.0, S)
        ok = ("?" in last or len(re.findall(r"https?://\S+", last)) == 1 or bool(_TAKEAWAY.search(last))
              or len(tail.split()) >= 6)
        return Check("post_ends_with_question", ok, "" if ok else "ends on no question, takeaway or link", 1.0, S)

    def _c_post_results_have_n() -> Check:
        if (c := _need_post("post_results_have_n")):
            return c
        from .. import factgate
        seen_n = False          # a sample size stated earlier, in a sentence that is not itself a result
        bad = []
        for sent in re.split(r"(?<=[.!?])\s+|\n+", body):
            is_result = factgate.RESULT_SHAPE.search(sent.lower()) or "%" in sent or "{{" in sent
            if is_result:
                if not (_HAS_N.search(sent) or seen_n):
                    bad.append(sent.strip())
            elif factgate.SETUP_COUNT.search(sent.lower()):
                seen_n = True
        return Check("post_results_have_n", not bad,
                     f"result without a sample size: {bad[0][:70]}" if bad else "", 1.0, S)

    def _c_post_states_limit() -> Check:
        ok = bool(post and _LIMIT.search(body))
        return _need_post("post_states_limit") or Check("post_states_limit", ok,
                                                        "" if ok else "no limit of the test stated", 1.0, S)

    def _c_post_not_copied_from_examples() -> Check:
        if (c := _need_post("post_not_copied_from_examples", 2.0)):
            return c
        from .. import examples  # late: pulls config
        run = examples.shared_run(body, examples.load())
        return Check("post_not_copied_from_examples", not run, f'copies an example: "{run[:60]}"' if run else "", 2.0, S)

    def _c_post_hashtag_count() -> Check:
        n = len(_HASHTAG.findall(body))
        return _need_post("post_hashtag_count") or Check("post_hashtag_count", n <= MAX_HASHTAGS,
                                                         f"{n} hashtags, max {MAX_HASHTAGS}", 1.0, S)

    def _c_post_first_comment() -> Check:
        m = re.search(r"^##\s+\d+\.\s*First comment[^\n]*\n(.*?)(?=^## |\Z)", doc, re.M | re.S)
        ok = bool(m and m.group(1).strip())
        return Check("post_first_comment", ok, "" if ok else "no first-comment section", 1.0, S)

    return _guarded(S, [_c_post_human_score, _c_post_human_score_good, _c_post_hook_short, _c_post_length_ok,
                        _c_post_no_invented_claims, _c_post_uses_placeholders, _c_post_numbers_traceable,
                        _c_post_ends_with_question, _c_post_hashtag_count, _c_post_first_comment,
                        _c_post_results_have_n, _c_post_states_limit, _c_post_not_copied_from_examples])


# ---------- summary ----------

def _pct(checks: list[Check]) -> float:
    total = sum(c.weight for c in checks)
    return round(100 * sum(c.weight for c in checks if c.passed) / total, 1) if total > 0 else 0.0


def summarize(checks: list[Check]) -> dict:
    """Weighted pass percentage overall and per stage. `failed` lists checks that matter (weight >= 1)."""
    stages = list(dict.fromkeys(c.stage for c in checks))
    return {"score": _pct(checks),
            "by_stage": {s: _pct([c for c in checks if c.stage == s]) for s in stages},
            "failed": [c.name for c in checks if not c.passed and c.weight >= 1],
            "n": len(checks)}
