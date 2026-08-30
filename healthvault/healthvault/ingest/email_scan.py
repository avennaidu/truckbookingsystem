"""Read-only IMAP scan for the medical post you already receive.

Lab results, scripts, referral letters, scheme statements and hospital
invoices mostly arrive by email and then sit there. This walks a mailbox,
finds the medical-looking messages, saves their attachments into the
vault and queues them for review.

Three rules make this safe to point at a personal inbox:

* READ ONLY. It opens the mailbox with `readonly=True`, never sets
  flags, never moves or deletes, and never sends. The worst a bug can do
  is read a message it should not have.
* NOTHING LEAVES. Attachments are written to the local documents folder.
  No message content is transmitted anywhere.
* CREDENTIALS STAY LOCAL. The password lives in `config.json` (which is
  gitignored) or, better, in the `HEALTHVAULT_EMAIL_PASSWORD`
  environment variable. Use a provider app-password, not your real one.

Gmail/Outlook need an app-password with IMAP enabled; both refuse a
plain account password, which is a good default.
"""

import email
import imaplib
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path

from .. import db
from ..extract.classify import classify, find_date, find_observations
from ..extract.text import extract_text, sha256_of
from .base import ImportReport

#: Senders and subject words worth a look. Broad on purpose - precision
#: comes from the classifier and from you, not from the search.
DEFAULT_SENDERS = [
    "ampath", "lancet", "pathcare", "vermaak", "netcare", "mediclinic",
    "life healthcare", "discovery", "bonitas", "momentum health", "medihelp",
    "bestmed", "clicks", "dis-chem", "medipost", "healthid",
]
DEFAULT_SUBJECTS = [
    "results", "pathology", "lab", "prescription", "script", "referral",
    "discharge", "radiology", "scan", "x-ray", "vaccination", "immunisation",
    "medical", "claim", "statement", "consultation", "specialist",
]

ATTACHMENT_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".txt", ".csv", ".json", ".xml"}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class EmailScanner:
    """Connects, searches, and stages documents. One mailbox per run."""

    name = "Email"
    kind = "email"

    def __init__(self, host: str, user: str, password: str, port: int = 993,
                 mailbox: str = "INBOX", documents_dir: Path = Path("documents"),
                 senders: list[str] | None = None,
                 subjects: list[str] | None = None):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.mailbox = mailbox
        self.documents_dir = Path(documents_dir)
        self.senders = senders if senders is not None else DEFAULT_SENDERS
        self.subjects = subjects if subjects is not None else DEFAULT_SUBJECTS

    # -- connection ----------------------------------------------------

    def connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(self.user, self.password)
        conn.select(self.mailbox, readonly=True)      # never mutate the mailbox
        return conn

    def search_terms(self, since_days: int) -> list[str]:
        """One IMAP query per term - servers vary wildly on OR support."""
        since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        terms = [f'(SINCE "{since}" FROM "{s}")' for s in self.senders]
        terms += [f'(SINCE "{since}" SUBJECT "{s}")' for s in self.subjects]
        return terms

    # -- the scan ------------------------------------------------------

    def scan(self, conn: sqlite3.Connection, since_days: int = 365,
             limit: int = 200, dry_run: bool = False) -> ImportReport:
        report = ImportReport(source=f"{self.name} ({self.user})")
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        try:
            imap = self.connect()
        except (imaplib.IMAP4.error, OSError) as exc:
            report.note(f"could not connect: {exc}")
            return report

        source_id = db.add_source(conn, self.kind, f"email: {self.user}",
                                  mailbox=self.mailbox, since_days=since_days)
        seen_uids: set[bytes] = set()
        try:
            for term in self.search_terms(since_days):
                try:
                    status, data = imap.search(None, term)
                except imaplib.IMAP4.error:
                    continue
                if status != "OK":
                    continue
                for uid in (data[0] or b"").split():
                    if uid in seen_uids or len(seen_uids) >= limit:
                        continue
                    seen_uids.add(uid)
                    try:
                        self._handle(conn, imap, uid, source_id, report, dry_run)
                    except Exception as exc:                # one bad message
                        report.skipped += 1                 # must not end the scan
                        report.note(f"message {uid.decode()}: {exc}")
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass
        report.note(f"examined {len(seen_uids)} messages")
        return report

    def _handle(self, conn, imap, uid, source_id, report, dry_run) -> None:
        status, data = imap.fetch(uid, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            report.skipped += 1
            return
        message = email.message_from_bytes(data[0][1])
        subject = _decoded(message.get("Subject", ""))
        sender = _decoded(message.get("From", ""))
        sent = _message_date(message)

        attachments = list(self._attachments(message))
        if not attachments:
            report.skipped += 1
            return
        for filename, payload in attachments:
            if dry_run:
                report.note(f"would save {filename} from {sender}")
                report.staged += 1
                continue
            path = self._save(filename, payload, sent)
            text = extract_text(path)
            kind, confidence = classify(text, filename)
            doc = {
                "title": subject or filename,
                "kind": kind,
                "date": find_date(text) or sent,
                "path": str(path),
                "sha256": sha256_of(path),
                "text": text[:20000],
                "notes": f"from={sender}; file={filename}",
            }
            from .. import store
            report.add(store.stage(
                conn, source_id, "document", doc,
                confidence=max(confidence, 0.5),
                reason=f"email attachment from {sender}",
                dedup_key="doc|" + doc["sha256"]))

            # A lab report usually carries values worth having as data,
            # not just as a PDF. Staged separately so you can take the
            # document and refuse the readings, or the other way round.
            if kind == "lab_result":
                for observation in find_observations(text):
                    observation["panel"] = subject or filename
                    report.add(store.stage(
                        conn, source_id, "observation", observation,
                        confidence=0.55,
                        reason=f"read off {filename}",
                        dedup_key=("obs|" + observation["name"].lower() + "|"
                                   + observation["date"] + "|" + observation["value"])))

    def _attachments(self, message):
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            filename = _decoded(part.get_filename() or "")
            if "attachment" not in disposition.lower() and not filename:
                continue
            if Path(filename).suffix.lower() not in ATTACHMENT_SUFFIXES:
                continue
            payload = part.get_payload(decode=True)
            if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
                continue
            yield filename, payload

    def _save(self, filename: str, payload: bytes, sent: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:120] or "attachment"
        stem = f"{sent or 'undated'}_{safe}"
        path = self.documents_dir / stem
        counter = 1
        while path.exists():
            path = self.documents_dir / f"{Path(stem).stem}_{counter}{Path(stem).suffix}"
            counter += 1
        path.write_bytes(payload)
        return path


def _decoded(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _message_date(message) -> str:
    raw = message.get("Date", "")
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.date().isoformat() if parsed else ""
    except (TypeError, ValueError):
        return ""
