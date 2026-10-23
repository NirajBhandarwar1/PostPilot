"""PostPilot command line.

  python -m postpilot run              keep running: scout every 3 days, build when you reply
  python -m postpilot scout            one scout now (emails if Gmail is set up)
  python -m postpilot scout --no-email one scout, save to file only
  python -m postpilot options 4 2      3-5 build options for idea 2 of batch #4
  python -m postpilot build 4 2 --option 3   build option 3 of that idea (no --option: the rough idea)
  python -m postpilot check            check email replies once
  python -m postpilot status           show setup and recent batches
  python -m postpilot test-email       send yourself a test email
  python -m postpilot precommit        check files for secrets before you commit
  python -m postpilot eval [N]         replay the last N saved runs and score idea quality
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
import sys

from . import db, llm, mailer, pipeline
from .config import settings


def _setup_logging() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(settings.data_dir / "postpilot.log", encoding="utf-8")],
    )


def status() -> None:
    ok = lambda b: "OK" if b else "MISSING"
    plist = llm.providers()
    writers = llm.providers("writer")
    print(f"Model          : {ok(bool(plist))}  " + (" -> ".join(f"{p.name}:{p.model}" for p in plist) or "set GROQ_API_KEY or OLLAMA_MODEL in .env"))
    print(f"Writer         : " + (" -> ".join(f"{p.name}:{p.model}" for p in writers) or "same as Model"))
    print(f"Paid tokens    : {llm.paid_tokens_today():,} used today of {llm.daily_cap():,} (OPENAI_DAILY_TOKENS)")
    print(f"Gmail          : {ok(settings.has_email)}  ({settings.gmail_address or 'not set'})")
    print(f"Voice profile  : {ok(bool(pipeline._voice()))}  ({settings.voice_file})")
    print(f"Output folder  : {settings.output_dir}")
    print(f"Cycle          : every {settings.cycle_days:g} days, replies checked every {settings.check_minutes:g} min")
    print(f"Last scout     : {db.get_meta('last_scout_at', 'never')}")
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
    return 1 if broken else 0


def _need_batch(batch_id: int):
    if not db.get_batch(batch_id):
        sys.exit(f"No batch #{batch_id}. See `python -m postpilot status`.")


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
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("test-email")
    pc = sub.add_parser("precommit")
    pc.add_argument("paths", nargs="*")
    ev = sub.add_parser("eval")
    ev.add_argument("n", nargs="?", type=int, default=3)
    sub.add_parser("doctor")
    po = sub.add_parser("posted")
    po.add_argument("batch", type=int)
    po.add_argument("url")
    ib = sub.add_parser("inbound")
    ib.add_argument("batch", type=int)
    ib.add_argument("--recruiters", type=int, default=0)
    ib.add_argument("--founders", type=int, default=0)
    ib.add_argument("--other", type=int, default=0)
    st = sub.add_parser("stats")
    st.add_argument("batch", type=int)
    for f in ("impressions", "reactions", "comments", "profile-views"):
        st.add_argument(f"--{f}", type=int)
    bl = sub.add_parser("baseline")
    for f in ("profile-views", "followers", "recruiter-msgs"):
        bl.add_argument(f"--{f}", type=int, required=True)
    sub.add_parser("report")
    args = ap.parse_args()

    if args.cmd == "run":
        pipeline.run_forever()
    elif args.cmd == "scout":
        pipeline.scout(send_email=not args.no_email)
    elif args.cmd == "build":
        print(pipeline.build(args.batch, args.choice, send_email=not args.no_email, option=args.option))
    elif args.cmd == "options":
        pipeline.propose(args.batch, args.choice, send_email=not args.no_email)
    elif args.cmd == "check":
        pipeline.check_replies()
    elif args.cmd == "status":
        status()
    elif args.cmd == "precommit":
        from . import precommit
        sys.exit(precommit.run(args.paths or None))
    elif args.cmd == "eval":
        pipeline.replay(args.n)
    elif args.cmd == "doctor":
        sys.exit(doctor())
    elif args.cmd == "posted":
        _need_batch(args.batch)
        db.set_outcome(args.batch, posted_url=args.url, posted_at=db.now_iso())
        db.update_batch(args.batch, status="posted")
        print(f"Recorded post for batch #{args.batch}. Log replies later with `postpilot inbound {args.batch} ...`")
    elif args.cmd == "inbound":
        _need_batch(args.batch)
        db.set_outcome(args.batch, recruiters=args.recruiters, founders=args.founders, other=args.other)
        print(f"Batch #{args.batch}: {args.recruiters} recruiters, {args.founders} founders, {args.other} other.")
    elif args.cmd == "stats":
        _need_batch(args.batch)
        fields = {k.replace("-", "_"): v for k, v in
                  {"impressions": args.impressions, "reactions": args.reactions,
                   "comments": args.comments, "profile_views": args.profile_views}.items() if v is not None}
        db.set_outcome(args.batch, **fields)
        print(f"Saved stats for batch #{args.batch}: {fields}")
    elif args.cmd == "baseline":
        base = {"profile_views": args.profile_views, "followers": args.followers,
                "recruiter_msgs": args.recruiter_msgs, "at": db.now_iso()}
        db.set_meta("baseline", json.dumps(base))
        print(f"Baseline saved: {base}")
    elif args.cmd == "report":
        pipeline.report()
    elif args.cmd == "test-email":
        if not settings.has_email:
            sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")
        mailer.send("[PostPilot] test", "If you can read this, email is working.")
        print(f"Sent to {settings.to_email}")


if __name__ == "__main__":
    main()
