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
TAG_RE = re.compile(r"\[PostPilot #(\d+)(?:\.(\d+))?\]")   # "#8" = ideas, "#8.3" = options for idea 3


def subject_for(batch_id: int, text: str, idea: int | None = None) -> str:
    return f"[PostPilot #{batch_id}{'.' + str(idea) if idea else ''}] {text}"


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


def _own_words(body: str) -> str:
    """The user's own text, above the quoted email."""
    own = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^On .+wrote:$", s) or s.startswith("-----Original"):
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
    go = re.search(r"\b(go|now|direct)\b", text)
    return m.group(1) + (" go" if go else ""), ratings


def parse_choice(body: str) -> str | None:
    """Read the user's own words (above the quoted email) and return '1'..'3', 'skip', or None."""
    return parse_reply(body)[0]


def sender_verified(msg: email.message.Message, sender: str) -> bool:
    """Gmail's own check that the reply really came from `sender` (eng review O5): the
    Authentication-Results header must show dkim=pass for the sender's domain, or dmarc=pass
    with header.from equal to it. A forged From line fails both."""
    domain = sender.rsplit("@", 1)[-1].lower()
    results = " ".join(msg.get_all("Authentication-Results") or []).lower()
    if not results or not domain:
        return False
    dkim = re.search(rf"\bdkim=pass\b[^;]*header\.(?:i|d)=@?{re.escape(domain)}\b", results)
    dmarc = re.search(rf"\bdmarc=pass\b[^;]*header\.from={re.escape(domain)}\b", results)
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


def fetch_replies() -> list[tuple[int, int | None, str]]:
    """Return [(batch_id, idea_or_None, choice)] for unread replies from the user. idea is set
    for replies to an options email ("#8.3"). Marks them read."""
    found = []
    with imaplib.IMAP4_SSL("imap.gmail.com", timeout=30) as im:
        im.login(settings.gmail_address, settings.gmail_app_password)
        im.select("INBOX")
        _, data = im.search(None, '(UNSEEN SUBJECT "PostPilot #")')
        for num in data[0].split():
            _, msg_data = im.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            sender = parseaddr(msg.get("From", ""))[1].lower()
            tag = TAG_RE.search(msg.get("Subject", ""))
            # Only trust replies from the user's own address, to a PostPilot email.
            is_reply = bool(msg.get("In-Reply-To")) or msg.get("Subject", "").lower().startswith("re:")
            if not tag or not is_reply or sender != settings.to_email.lower():
                continue  # also skips PostPilot's own emails when you send to yourself
            if not sender_verified(msg, sender):
                im.store(num, "+FLAGS", "\\Seen")
                log.warning("ignored reply to #%s: failed Gmail sender check (dkim/dmarc)", tag.group(1))
                continue
            idea = int(tag.group(2)) if tag.group(2) else None
            choice, ratings = parse_reply(_plain_body(msg), _ideas_in_batch(int(tag.group(1)), idea))
            im.store(num, "+FLAGS", "\\Seen")
            if ratings and not idea:  # kept for the Stage 5 feedback report
                from . import db
                db.set_meta(f"ratings:{tag.group(1)}", json.dumps(ratings))
                log.info("ratings for #%s: %s", tag.group(1), ratings)
            if choice:
                found.append((int(tag.group(1)), idea, choice))
            else:
                log.warning("reply to #%s had no number or skip in it", tag.group(0))
    return found
