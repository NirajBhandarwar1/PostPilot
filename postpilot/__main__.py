"""PostPilot command line.

  python -m postpilot run              keep running: scout every day, build when you reply
  python -m postpilot scout            one scout now (emails if Gmail is set up)
  python -m postpilot scout --no-email one scout, save to file only
  python -m postpilot options 4 2      3-5 build options for idea 2 of batch #4
  python -m postpilot build 4 2 --option 3   build option 3 of that idea (no --option: the rough idea)
  python -m postpilot check            check email replies once
  python -m postpilot status           show setup and recent batches
  python -m postpilot test-email       send yourself a test email
  python -m postpilot precommit        check files for secrets before you commit
  python -m postpilot eval run         score the current prompts on the golden stories (also: capture, replay, list, history)
  python -m postpilot doctor           show how many signals each live source returns now

  After you publish a post (track what actually brings recruiters and founders):
  python -m postpilot posted 4 <url>   record the LinkedIn link for batch #4
  python -m postpilot inbound 4 --recruiters 2 --founders 1 --other 0
  python -m postpilot stats 4 --impressions 5000 --reactions 120 --comments 30 --profile-views 80
  python -m postpilot baseline --profile-views 40 --followers 900 --recruiter-msgs 1
  python -m postpilot report           which formats and topics bring the most inbound
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from . import db, llm, mailer, pipeline, reports
from .config import ROOT, settings


def _setup_logging() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # TextIO stub lacks reconfigure
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(settings.data_dir / "postpilot.log", encoding="utf-8")],
    )


def status() -> None:
    def ok(b):
        return "OK" if b else "MISSING"
    plist = llm.providers()
    writers = llm.providers("writer")
    print(f"Model          : {ok(bool(plist))}  " + (" -> ".join(f"{p.name}:{p.model}" for p in plist) or "set GROQ_API_KEY or OLLAMA_MODEL in .env"))
    print("Writer         : " + (" -> ".join(f"{p.name}:{p.model}" for p in writers) or "same as Model"))
    print(f"Paid tokens    : {llm.paid_tokens_today():,} used today of {llm.daily_cap():,} (OPENAI_DAILY_TOKENS)")
    for line in llm.quota_lines() or ["no free-tier use recorded today"]:
        print(f"Free quota     : {line}")
    print(f"Gmail          : {ok(settings.has_email)}  ({settings.gmail_address or 'not set'})")
    print(f"Voice profile  : {ok(bool(pipeline.voice()))}  ({settings.voice_file})")
    print(f"Output folder  : {settings.output_dir}")
    print(f"Cycle          : every {settings.cycle_days:g} days, replies checked every {settings.check_minutes:g} min")
    print(f"Loop           : {pipeline.loop_line()}")
    print(f"Last scout     : {db.get_meta('last_scout_at', 'never')}")
    rates = db.llm_pass_rates(7)
    if rates:
        print("\nLast 7 days (model calls that gave usable output):")
        for (prov, model, role), (good, total) in sorted(rates.items()):
            short = model.rsplit("/", 1)[-1][:32]       # the tail names the model; the vendor prefix is noise
            print(f"  {prov:12} {short:32} {role:8} {good}/{total}")
    print("\nRecent batches:")
    for b in db.recent_batches(5):
        titles = [i["title"] for i in json.loads(b["ideas_json"])["ideas"]]
        print(f"  #{b['id']} {b['created_at'][:10]} {b['status']:8} chosen={b['chosen']}  {titles}")


def doctor() -> int:
    """Live check of every trend source (eng review R2): a 0 means that source is broken today."""
    from . import sources
    broken = 0
    for fn in sources.COLLECTORS:
        try:
            got = fn()
            sample = got[0]["title"][:70] if got else ""
            print(f"{'OK ' if got else 'ZERO'} {fn.__name__:20} {len(got):>3} signals  {sample}")
            broken += 0 if got else 1
        except Exception as e:
            print(f"FAIL {fn.__name__:20}   error: {str(e)[:80]}")
            broken += 1
    print("\nAll sources returning data." if not broken else f"\n{broken} source(s) need attention.")
    # D21: a backup writer is only useful if it really answers strict JSON today.
    backups = llm.providers("writer")[1:]
    if backups:
        print("\nBackup writers (one tiny test call each):")
        for p in backups:
            result = llm.probe(p.name)
            print(f"{'OK ' if result == 'ok' else 'FAIL'} {p.name:12} {result}")
            broken += 0 if result == "ok" else 1
    return 1 if broken else 0


def _need_batch(batch_id: int):
    if not db.get_batch(batch_id):
        sys.exit(f"No batch #{batch_id}. See `python -m postpilot status`.")


# ---------- secret check before you commit (was precommit.py) ----------
# It refuses when a file is `.env`, contains one of the real secret values from `.env`, or contains
# something shaped like an API key. Secret values are never printed. You run it by hand; nothing
# commits for you.

# Settings in .env that are configuration, not secrets. Their values (model names, provider
# lists) legitimately appear in code and docs, so matching them would block every commit.
NOT_SECRET = re.compile(
    r"^(LLM_PROVIDERS|WRITER_PROVIDERS|(?:SCOUT|IDEAS|OPTIONS|BRIEF|POST)_PROVIDERS|\w+_MODEL|(?:\w+_)?REASONING_EFFORT|USER_SETUP|CURRENT_MODELS|CYCLE_DAYS|"
    r"CHECK_MINUTES|RADAR_DAYS|OUTPUT_DIR|VOICE_FILE|OLLAMA_URL|CUSTOM_BASE_URL|CUSTOM_STRICT|"
    r"OPENAI_DAILY_TOKENS|CLOUDFLARE_DAILY_NEURONS|GROQ_DAILY_TOKENS|PAID_EXPERIMENTS)$")

# Known key shapes: a prefix plus a long run of key characters (bare prefixes like "sk-" or
# "hf_" would also match ordinary words such as "risk-" or "hf_api").
KEY_SHAPES = re.compile(
    r"(sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}|cfut_[A-Za-z0-9_]{20,})")

SKIP_DIRS = {".venv", ".git", "data", ".gstack", "__pycache__"}


def secret_values(env_path: Path) -> dict[str, str]:
    """Name -> value for every .env entry that is a secret (6+ characters)."""
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.split(" #")[0].strip().strip('"').strip("'")
        if len(value) >= 6 and not NOT_SECRET.match(name):
            out[name] = value
    return out


def staged_files(root: Path) -> list[Path] | None:
    """Files staged in git, or None when the folder is not a git repo yet."""
    try:
        res = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=root,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return [root / p for p in res.stdout.splitlines() if p.strip()]


def _ignored_names(root: Path) -> set[str]:
    """Plain names from .gitignore (e.g. `.env`, `data/`), which git would never stage."""
    gi = root / ".gitignore"
    if not gi.exists():
        return set()
    names = set()
    for line in gi.read_text(encoding="utf-8").splitlines():
        line = line.strip().rstrip("/")
        if line and not line.startswith("#") and "*" not in line:
            names.add(line)
    return names


def project_files(root: Path) -> list[Path]:
    """Every file `git add .` would pick up (no git repo yet), so the check is still useful."""
    skip = SKIP_DIRS | _ignored_names(root)
    return [p for p in root.rglob("*")
            if p.is_file() and not (set(p.relative_to(root).parts) & skip)
            and not p.name.endswith((".pyc",))]


def scan_for_secrets(files: list[Path], secrets: dict[str, str]) -> list[str]:
    """Problems found, described without ever showing a secret value."""
    problems = []
    for f in files:
        if f.name == ".env":
            problems.append(f"{f}: the .env file itself must never be committed")
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, value in secrets.items():
            if value in text:
                problems.append(f"{f}: contains the value of {name} from .env")
        m = KEY_SHAPES.search(text)
        if m:
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{f}:{line_no}: something shaped like an API key ({m.group(0)[:6]}...)")
    return problems


def precommit(paths: list[str] | None = None, root: Path = ROOT) -> int:
    secrets = secret_values(root / ".env")
    if paths:
        files, where = [Path(p).resolve() for p in paths], "the given files"
    else:
        staged = staged_files(root)
        files, where = (staged, "staged files") if staged is not None else (project_files(root), "all project files (no git repo yet)")
    problems = scan_for_secrets(files, secrets)
    print(f"Checked {len(files)} {where} against {len(secrets)} secret values from .env")
    if problems:
        print("BLOCKED. Do not commit until these are fixed:")
        for p in problems:
            print("  -", p)
        return 1
    print("OK: no secrets found.")
    return 0


def main() -> None:
    _setup_logging()
    ap = argparse.ArgumentParser(prog="postpilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("scout")
    s.add_argument("--no-email", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("batch", type=int)
    b.add_argument("choice", type=int)
    b.add_argument("--option", type=int, help="build this option from the options email")
    b.add_argument("--no-email", action="store_true")
    op = sub.add_parser("options")
    op.add_argument("batch", type=int)
    op.add_argument("choice", type=int)
    op.add_argument("--no-email", action="store_true")
    op.add_argument("--fresh", action="store_true", help="make a new options list instead of resending the saved one")
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("test-email")
    pc = sub.add_parser("precommit")
    pc.add_argument("paths", nargs="*")
    ev = sub.add_parser("eval").add_subparsers(dest="eval_cmd", required=True)
    er = ev.add_parser("run", help="score the current pipeline on the golden stories (free models)")
    er.add_argument("--story", action="append", help="a golden story name (repeatable; default: all)")
    er.add_argument("--max", type=int, default=3, help="most stories to run (default 3)")
    er.add_argument("--no-fail", action="store_true", help="exit 0 even when the score drops")
    ec = ev.add_parser("capture", help="save a story from a saved batch as a golden story")
    ec.add_argument("batch", type=int)
    ec.add_argument("idea", type=int)
    ec.add_argument("option", type=int)
    ec.add_argument("name")
    ep = ev.add_parser("replay", help="re-score a saved run offline (no model calls)")
    ep.add_argument("run_file", nargs="?", help="a file or name in evals/results (default: the last run)")
    ev.add_parser("list", help="golden stories and their last scores")
    el = ev.add_parser("live", help="signals from real use: pick rate, your ratings, model health, results")
    el.add_argument("days", nargs="?", type=int, default=14)
    eh = ev.add_parser("history", help="fact-check stats over the last N saved scouts")
    eh.add_argument("n", nargs="?", type=int, default=3)
    sub.add_parser("doctor")
    po = sub.add_parser("posted")
    po.add_argument("batch", type=int)
    po.add_argument("url")
    po.add_argument("--build", type=int, help="which build of the batch you posted (default: the latest; "
                                               "BUILD.md's 'After you post' shows the number)")
    ib = sub.add_parser("inbound")
    ib.add_argument("batch", type=int)
    ib.add_argument("--recruiters", type=int, default=0)
    ib.add_argument("--founders", type=int, default=0)
    ib.add_argument("--other", type=int, default=0)
    ib.add_argument("--build", type=int, help="which build (default: the one you recorded as posted)")
    st = sub.add_parser("stats")
    st.add_argument("batch", type=int)
    for f in ("impressions", "reactions", "comments", "profile-views"):
        st.add_argument(f"--{f}", type=int)
    st.add_argument("--build", type=int, help="which build (default: the one you recorded as posted)")
    bl = sub.add_parser("baseline")
    for f in ("profile-views", "followers", "recruiter-msgs"):
        bl.add_argument(f"--{f}", type=int, required=True)
    sub.add_parser("report")
    args = ap.parse_args()
    try:
        _run(args)
    except ValueError as e:              # bad batch / idea / option / --build number
        sys.exit(str(e))


def _run(args) -> None:
    if args.cmd == "run":
        pipeline.run_forever()
    elif args.cmd == "scout":
        pipeline.scout(send_email=not args.no_email)
    elif args.cmd == "build":
        print(pipeline.build(args.batch, args.choice, send_email=not args.no_email, option=args.option,
                             reuse=False))
    elif args.cmd == "options":
        pipeline.propose(args.batch, args.choice, send_email=not args.no_email, fresh=args.fresh)
    elif args.cmd == "check":
        pipeline.check_replies()
    elif args.cmd == "status":
        status()
    elif args.cmd == "precommit":
        sys.exit(precommit(args.paths or None))
    elif args.cmd == "eval":
        if args.eval_cmd == "history":
            reports.replay(args.n)
        elif args.eval_cmd == "live":
            from .evals import live
            print(live.render(live.signals(args.days)))
        else:
            from .evals import runner
            sys.exit(runner.cli(args))
    elif args.cmd == "doctor":
        sys.exit(doctor())
    elif args.cmd == "posted":
        _need_batch(args.batch)
        n = db.set_outcome(args.batch, args.build, posted_url=args.url, posted_at=db.now_iso())
        db.update_batch(args.batch, status="posted")
        print(f"Recorded post for batch #{args.batch}, build {n}. "
              f"Log replies later with `postpilot inbound {args.batch} ...`")
    elif args.cmd == "inbound":
        _need_batch(args.batch)
        n = db.set_outcome(args.batch, args.build, recruiters=args.recruiters, founders=args.founders,
                           other=args.other)
        print(f"Batch #{args.batch}, build {n}: {args.recruiters} recruiters, {args.founders} founders, "
              f"{args.other} other.")
    elif args.cmd == "stats":
        _need_batch(args.batch)
        fields = {k.replace("-", "_"): v for k, v in
                  {"impressions": args.impressions, "reactions": args.reactions,
                   "comments": args.comments, "profile_views": args.profile_views}.items() if v is not None}
        n = db.set_outcome(args.batch, args.build, **fields)
        print(f"Saved stats for batch #{args.batch}, build {n}: {fields}")
    elif args.cmd == "baseline":
        base = {"profile_views": args.profile_views, "followers": args.followers,
                "recruiter_msgs": args.recruiter_msgs, "at": db.now_iso()}
        db.set_meta("baseline", json.dumps(base))
        print(f"Baseline saved: {base}")
    elif args.cmd == "report":
        reports.report()
    elif args.cmd == "test-email":
        if not settings.has_email:
            sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")
        mailer.send("[PostPilot] test", "If you can read this, email is working.")
        print(f"Sent to {settings.to_email}")


if __name__ == "__main__":
    main()
