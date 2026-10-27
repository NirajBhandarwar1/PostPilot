"""Gmail over SMTP (send) and IMAP (read replies), using a Gmail App Password.

No Google Cloud project needed: turn on 2-Step Verification, then create an App Password at
https://myaccount.google.com/apppasswords and put it in .env as GMAIL_APP_PASSWORD.
"""

import email
import imaplib
import json
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from .config import settings

log = logging.getLogger("postpilot.mail")
# "#8" = ideas, "#8.3" = options for idea 3, "#8.3b" = a fresh options list for idea 3.
# Emails you can't pick from (the build plan, notes) carry a word after the number, e.g.
# "[PostPilot #8.3 plan]", which this pattern does not match, so replies to them never start work.
TAG_RE = re.compile(r"\[PostPilot #(\d+)(?:\.(\d+)([a-z])?)?\]")


def subject_for(batch_id: int, text: str, idea: int | None = None, version: str = "", kind: str = "") -> str:
    ref = f"#{batch_id}{'.' + str(idea) + version if idea else ''}" + (f" {kind}" if kind else "")
    return f"[PostPilot {ref}] {text}"


def send(subject: str, body: str, html: str | None = None,
         attachments: list[tuple[str, str]] = ()) -> str:
    msg = EmailMessage()
    msg["From"] = settings.gmail_address
    msg["To"] = settings.to_email
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="postpilot.local")
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, text in attachments:          # e.g. ("BUILD.md", "...") for pasting into Cursor
        msg.add_attachment(text.encode("utf-8"), maintype="text", subtype="markdown", filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(settings.gmail_address, settings.gmail_app_password)
        s.send_message(msg)
    log.info("sent: %s", subject)
    return msg["Message-ID"]


def _plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


QUOTE_START = re.compile(r"^(?:>|-----\s*Original|_{5,}|(?:From|Sent|To|Subject|Date):\s)", re.I)


def _own_words(body: str) -> str:
    """The user's own text, above the quoted email. Stops at '>' quotes, 'On ... wrote:' (also
    when Gmail wraps it over two lines) and Outlook's From:/Sent: header block."""
    own = []
    lines = [ln.strip() for ln in body.splitlines()]
    for i, s in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if QUOTE_START.match(s) or (s.startswith("On ") and (s.endswith("wrote:") or nxt.endswith("wrote:"))):
            break
        own.append(s)
    return " ".join(own).lower()


def parse_reply(body: str, n_ideas: int = 3) -> tuple[str | None, list[int] | None]:
    """(choice, ratings): choice is '1'..'5' (optionally '3 go'), 'skip' or None; ratings are the
    optional 'rate 4 5 2' scores, kept only with exactly one number (1-5) per item shown."""
    text = _own_words(body)
    ratings = None
    m = re.search(r"\brate\b([\s\d,]*)", text)
    if m:
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        if len(nums) == n_ideas and all(1 <= n <= 5 for n in nums):
            ratings = nums
        text = text[:m.start()]                 # the pick comes before "rate"
    if re.search(r"\b(skip|none|pass)\b", text):
        return "skip", ratings
    m = re.search(rf"\b([1-{max(1, min(n_ideas, 9))}])\b", text)
    if not m:
        return None, ratings
    # "3 new" / "3 fresh" asks for a paid new options list, so the word must sit right after the
    # number: "2, the new one looks good" stays a plain pick.
    if re.search(rf"\b{m.group(1)}\s*(?:new|fresh|redo)\b", text):
        return m.group(1) + " new", ratings
    # "2 go" pays for a build right away, so it also has to follow the number:
    # "2, let's go with the cheaper one" stays a plain pick.
    go = re.search(rf"\b{m.group(1)}\s*(?:go|now|direct)\b", text)
    return m.group(1) + (" go" if go else ""), ratings


def sender_verified(msg: email.message.Message, sender: str) -> bool:
    """Gmail's own check that the reply really came from `sender` (eng review O5): the
    Authentication-Results header must show dkim=pass for the sender's domain, or dmarc=pass
    with header.from equal to it. A forged From line fails both."""
    domain = sender.rsplit("@", 1)[-1].lower()
    # Only Gmail's own result counts: it is the TOP Authentication-Results header and names
    # exactly mx.google.com. A sender can add lines of its own further down (or name a lookalike).
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        # Mail you send from your own Gmail account to yourself never goes through Gmail's incoming
        # checks, so it has no such header. Mail from outside always gets one, so a missing header
        # means the message came from inside your own account (seen on a real reply, 2026-10-28).
        return bool(settings.gmail_address) and sender.lower() == settings.gmail_address.lower()
    top = str(headers[0]).strip().lower()
    if top.split(";", 1)[0].strip() != "mx.google.com" or not domain:
        return False
    results = top
    end = r"(?=[\s;)]|$)"                   # gmail.com, not gmail.com.attacker.tld
    dkim = re.search(rf"\bdkim=pass\b[^;]*header\.(?:i|d)=@?{re.escape(domain)}{end}", results)
    dmarc = re.search(rf"\bdmarc=pass\b[^;]*header\.from={re.escape(domain)}{end}", results)
    return bool(dkim or dmarc)


def _ideas_in_batch(batch_id: int, idea: int | None = None) -> int:
    """How many numbered items that email showed: ideas, or the options for one idea."""
    from . import db
    row = db.get_batch(batch_id)
    if not row:
        return 3
    data = json.loads(row["ideas_json"])
    if idea:
        return len((data.get("options") or {}).get(str(idea), {}).get("options", [])) or 5
    return len(data.get("ideas", []))


def _imap():
    im = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    im.login(settings.gmail_address, settings.gmail_app_password)
    im.select("INBOX")
    return im


def fetch_replies() -> list[tuple[int, str | None, str, bytes]]:
    """Return [(batch_id, idea_ref, choice, uid)] for unread replies from the user. idea_ref is
    "3" or "3b" for replies to an options email ("#8.3", "#8.3b"), None for the ideas email.

    Messages are read with BODY.PEEK, so nothing is marked read here: a reply is marked read only
    after it has been handled (mark_read), so a run killed mid-build redoes it (eng review D8).
    PostPilot's own emails to you are never touched. Replies that can't be acted on (failed
    sender check, no number in them) are marked read at once so they aren't re-read forever."""
    found, ignore = [], []
    with _imap() as im:                      # logs out on exit, even after an error
        _, data = im.uid("SEARCH", None, '(UNSEEN SUBJECT "PostPilot #")')
        for uid in (data[0] or b"").split():
            _, head = im.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
            if not head or not isinstance(head[0], tuple):
                continue                     # deleted between SEARCH and FETCH
            hmsg = email.message_from_bytes(head[0][1])
            sender = parseaddr(hmsg.get("From", ""))[1].lower()
            tag = TAG_RE.search(hmsg.get("Subject", ""))
            # Only trust replies from the user's own address, to a PostPilot email.
            is_reply = bool(hmsg.get("In-Reply-To")) or hmsg.get("Subject", "").lower().startswith("re:")
            if not tag or not is_reply or sender != settings.to_email.lower():
                continue  # PostPilot's own emails stay unread for you to read
            _, full = im.uid("FETCH", uid, "(BODY.PEEK[])")
            if not full or not isinstance(full[0], tuple):
                continue
            msg = email.message_from_bytes(full[0][1])
            if not sender_verified(msg, sender):
                ignore.append(uid)
                log.warning("ignored reply to #%s: failed Gmail sender check (dkim/dmarc)", tag.group(1))
                continue
            idea = int(tag.group(2)) if tag.group(2) else None
            ref = f"{idea}{tag.group(3) or ''}" if idea else None      # "3" or "3b" for options emails
            choice, ratings = parse_reply(_plain_body(msg), _ideas_in_batch(int(tag.group(1)), idea))
            if ratings and not idea:  # kept for the Stage 5 feedback report (same value if re-read)
                from . import db
                db.set_meta(f"ratings:{tag.group(1)}", json.dumps(ratings))
                log.info("ratings for #%s: %s", tag.group(1), ratings)
            if choice:
                found.append((int(tag.group(1)), ref, choice, uid))
            else:
                ignore.append(uid)
                log.warning("reply to %s had no number or skip in it", tag.group(0))
        for uid in ignore:
            im.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    return found


def mark_read(uids: list[bytes]) -> None:
    """Mark handled replies read, over a fresh connection (UIDs stay valid across sessions)."""
    if not uids:
        return
    with _imap() as im:
        for uid in uids:
            im.uid("STORE", uid, "+FLAGS", "(\\Seen)")
