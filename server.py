"""
MCP Email Server for Proton Mail Bridge.

A production-quality MCP server providing email access via IMAP/SMTP
through Proton Mail Bridge. Runs over stdio transport.

Configuration via .env file or environment variables:
    EMAIL_USER  - IMAP/SMTP username
    EMAIL_PASS  - IMAP/SMTP password
    IMAP_HOST   - IMAP server host (default: 127.0.0.1)
    IMAP_PORT   - IMAP server port (default: 1143)
    SMTP_HOST   - SMTP server host (default: 127.0.0.1)
    SMTP_PORT   - SMTP server port (default: 1025)
"""

import imaplib
import smtplib
import email
import email.utils
import email.header
import email.mime.text
import email.mime.multipart
import json
import os
import re
import threading
import time
import logging
from datetime import datetime, timezone
from email.header import decode_header as _decode_header
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from same directory as this script
load_dotenv(Path(__file__).parent / ".env")

from db import EmailDB, EmailSyncer, ATTACHMENTS_DIR, SYNC_MAILBOXES

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("email-server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
IMAP_HOST = os.environ.get("IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "1143"))
SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))

MAX_BODY_LENGTH = 50_000

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    """Strips HTML tags and extracts text content."""

    def __init__(self):
        super().__init__()
        self._result = StringIO()
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._result.write("\n")

    def handle_data(self, data):
        if not self._skip:
            self._result.write(data)

    def get_text(self) -> str:
        return self._result.getvalue()


def strip_html(html_content: str) -> str:
    """Strip HTML tags and return plain text."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        # Fallback: crude regex strip
        return re.sub(r"<[^>]+>", "", html_content)
    text = extractor.get_text()
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# RFC 2047 header decoding
# ---------------------------------------------------------------------------

def decode_header_value(value: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header value into a unicode string."""
    if value is None:
        return ""
    parts = []
    for decoded_bytes, charset in _decode_header(value):
        if isinstance(decoded_bytes, bytes):
            encoding = charset or "utf-8"
            try:
                parts.append(decoded_bytes.decode(encoding, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(decoded_bytes.decode("utf-8", errors="replace"))
        else:
            parts.append(str(decoded_bytes))
    return "".join(parts)


# ---------------------------------------------------------------------------
# MIME body extraction
# ---------------------------------------------------------------------------

def extract_body(msg: email.message.Message) -> str:
    """Extract text body from an email message.

    Prefers text/plain. Falls back to text/html with tags stripped.
    """
    text_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text_parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        text_parts.append(payload.decode("utf-8", errors="replace"))
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        html_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                text_parts.append(decoded)
            elif content_type == "text/html":
                html_parts.append(decoded)

    if text_parts:
        body = "\n".join(text_parts)
    elif html_parts:
        body = strip_html("\n".join(html_parts))
        body = "[Converted from HTML]\n\n" + body
    else:
        body = "(No text content)"

    if len(body) > MAX_BODY_LENGTH:
        body = body[:MAX_BODY_LENGTH] + f"\n\n... [truncated at {MAX_BODY_LENGTH} chars]"

    return body


def _sanitize_filename(name: str) -> str:
    """Remove RFC 2047 header folding artifacts from filenames."""
    # Strip \r\n and collapse extra whitespace
    name = name.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    name = re.sub(r" {2,}", " ", name).strip()
    return name


def list_attachments(msg: email.message.Message) -> list[dict]:
    """List attachment metadata from a message."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                filename = part.get_filename()
                if filename:
                    filename = _sanitize_filename(decode_header_value(filename))
                content_type = part.get_content_type()
                size = len(part.get_payload(decode=True) or b"")
                attachments.append({
                    "filename": filename or "(unnamed)",
                    "content_type": content_type,
                    "size_bytes": size,
                })
    return attachments


# ---------------------------------------------------------------------------
# IMAP Connection Pool
# ---------------------------------------------------------------------------

class IMAPConnectionPool:
    """Thread-safe IMAP connection pool with keepalive.

    Maintains a single reusable connection. If the connection goes stale,
    it is replaced transparently.
    """

    def __init__(self, host: str, port: int, user: str, password: str):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._conn: Optional[imaplib.IMAP4] = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._selected_mailbox: Optional[str] = None

    def _create_connection(self) -> imaplib.IMAP4:
        """Create a fresh IMAP connection and authenticate."""
        conn = imaplib.IMAP4(self._host, self._port)
        conn.login(self._user, self._password)
        self._selected_mailbox = None
        return conn

    def _is_alive(self, conn: imaplib.IMAP4) -> bool:
        """Check if connection is still usable via NOOP."""
        try:
            status, _ = conn.noop()
            return status == "OK"
        except Exception:
            return False

    def get_connection(self) -> imaplib.IMAP4:
        """Get a working IMAP connection (creates or reuses)."""
        with self._lock:
            if self._conn is not None:
                # Stale check: if idle > 4 minutes, reconnect
                if time.time() - self._last_used > 240:
                    try:
                        self._conn.logout()
                    except Exception:
                        pass
                    self._conn = None
                elif not self._is_alive(self._conn):
                    try:
                        self._conn.logout()
                    except Exception:
                        pass
                    self._conn = None

            if self._conn is None:
                self._conn = self._create_connection()
                self._selected_mailbox = None

            self._last_used = time.time()
            return self._conn

    def select_mailbox(self, mailbox: str = "INBOX", readonly: bool = True) -> imaplib.IMAP4:
        """Get connection with the specified mailbox selected."""
        conn = self.get_connection()
        # Always re-select to ensure fresh state
        with self._lock:
            if readonly:
                status, data = conn.select(mailbox, readonly=True)
            else:
                status, data = conn.select(mailbox)
            if status != "OK":
                raise RuntimeError(f"Failed to select mailbox '{mailbox}': {data}")
            self._selected_mailbox = mailbox
        return conn

    def invalidate(self):
        """Force-close the current connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.logout()
                except Exception:
                    pass
                self._conn = None
                self._selected_mailbox = None

    def execute_with_retry(self, fn, *args, **kwargs):
        """Execute a function with one retry on connection failure."""
        try:
            return fn(*args, **kwargs)
        except (imaplib.IMAP4.error, OSError, ConnectionError, BrokenPipeError):
            self.invalidate()
            return fn(*args, **kwargs)


# Global pool instance (initialized lazily)
_pool: Optional[IMAPConnectionPool] = None
_pool_lock = threading.Lock()


def get_pool() -> IMAPConnectionPool:
    """Get or create the global IMAP connection pool."""
    global _pool
    with _pool_lock:
        if _pool is None:
            if not EMAIL_USER or not EMAIL_PASS:
                raise RuntimeError(
                    "EMAIL_USER and EMAIL_PASS environment variables must be set"
                )
            _pool = IMAPConnectionPool(IMAP_HOST, IMAP_PORT, EMAIL_USER, EMAIL_PASS)
        return _pool


# ---------------------------------------------------------------------------
# Local DB singleton
# ---------------------------------------------------------------------------

_db: Optional[EmailDB] = None
_db_lock = threading.Lock()


def get_db() -> EmailDB:
    """Get or create the global EmailDB instance."""
    global _db
    with _db_lock:
        if _db is None:
            _db = EmailDB()
        return _db


# ---------------------------------------------------------------------------
# IMAP helper functions (synchronous)
# ---------------------------------------------------------------------------

def _imap_list_mailboxes() -> list[dict]:
    """List all available mailboxes."""
    pool = get_pool()
    conn = pool.get_connection()
    status, data = conn.list()
    if status != "OK":
        raise RuntimeError(f"LIST failed: {data}")

    mailboxes = []
    for item in data:
        if item is None:
            continue
        decoded = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        # Parse IMAP LIST response: (\\flags) "delimiter" "name"
        match = re.match(r'\(([^)]*)\)\s+"([^"]+)"\s+"?([^"]+)"?', decoded)
        if match:
            flags = match.group(1)
            delimiter = match.group(2)
            name = match.group(3).strip('"')
            mailboxes.append({
                "name": name,
                "flags": flags,
                "delimiter": delimiter,
            })
    return mailboxes


def _imap_search(mailbox: str, criteria: str) -> list[bytes]:
    """Search for messages matching criteria. Returns list of message UIDs."""
    pool = get_pool()
    conn = pool.select_mailbox(mailbox, readonly=True)
    status, data = conn.uid("SEARCH", None, criteria)
    if status != "OK":
        raise RuntimeError(f"SEARCH failed: {data}")
    if not data or not data[0]:
        return []
    return data[0].split()


def _imap_fetch_headers(mailbox: str, uids: list[bytes]) -> list[dict]:
    """Fetch headers for a list of UIDs. Returns list of metadata dicts."""
    if not uids:
        return []

    pool = get_pool()
    conn = pool.select_mailbox(mailbox, readonly=True)

    uid_str = b",".join(uids)
    status, data = conn.uid(
        "FETCH", uid_str, "(UID FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID)])"
    )
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")

    # Build a map of requested UID order for sorting
    uid_order = {uid.decode() if isinstance(uid, bytes) else str(uid): idx for idx, uid in enumerate(uids)}

    results = []
    i = 0
    while i < len(data):
        item = data[i]
        if isinstance(item, tuple) and len(item) == 2:
            meta_line = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
            header_bytes = item[1]

            # Extract UID
            uid_match = re.search(r"UID\s+(\d+)", meta_line)
            uid = uid_match.group(1) if uid_match else "?"

            # Extract FLAGS
            flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", meta_line)
            flags = flags_match.group(1) if flags_match else ""

            # Parse headers
            msg = email.message_from_bytes(header_bytes)
            subject = decode_header_value(msg.get("Subject"))
            from_addr = decode_header_value(msg.get("From"))
            to_addr = decode_header_value(msg.get("To"))
            date_str = msg.get("Date", "")
            message_id = msg.get("Message-ID", "")

            results.append({
                "uid": uid,
                "subject": subject,
                "from": from_addr,
                "to": to_addr,
                "date": date_str,
                "message_id": message_id,
                "flags": flags,
                "is_read": "\\Seen" in flags,
                "is_flagged": "\\Flagged" in flags,
            })
            i += 2  # Skip the closing b')' line
        else:
            i += 1

    # Sort results to match the requested UID order (IMAP FETCH may return
    # results in arbitrary order)
    results.sort(key=lambda r: uid_order.get(r["uid"], len(uid_order)))
    return results


def _imap_fetch_full(mailbox: str, uid: str) -> dict:
    """Fetch full message by UID."""
    pool = get_pool()
    conn = pool.select_mailbox(mailbox, readonly=True)

    status, data = conn.uid("FETCH", uid.encode(), "(UID FLAGS RFC822)")
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")

    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            meta_line = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
            raw_email = item[1]

            # Extract FLAGS
            flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", meta_line)
            flags = flags_match.group(1) if flags_match else ""

            msg = email.message_from_bytes(raw_email)
            body = extract_body(msg)
            attachments = list_attachments(msg)

            return {
                "uid": uid,
                "subject": decode_header_value(msg.get("Subject")),
                "from": decode_header_value(msg.get("From")),
                "to": decode_header_value(msg.get("To")),
                "cc": decode_header_value(msg.get("Cc")),
                "bcc": decode_header_value(msg.get("Bcc")),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "in_reply_to": msg.get("In-Reply-To", ""),
                "references": msg.get("References", ""),
                "flags": flags,
                "is_read": "\\Seen" in flags,
                "is_flagged": "\\Flagged" in flags,
                "body": body,
                "attachments": attachments,
            }

    raise RuntimeError(f"Message with UID {uid} not found")


def _imap_store_flags(mailbox: str, uid: str, flags: str, action: str):
    """Set/remove flags on a message. action is '+FLAGS' or '-FLAGS'."""
    pool = get_pool()
    conn = pool.select_mailbox(mailbox, readonly=False)
    status, data = conn.uid("STORE", uid.encode(), action, flags)
    if status != "OK":
        raise RuntimeError(f"STORE flags failed: {data}")


def _imap_move(uid: str, source_mailbox: str, dest_mailbox: str):
    """Move a message from one mailbox to another (copy + delete)."""
    pool = get_pool()
    conn = pool.select_mailbox(source_mailbox, readonly=False)

    # Copy to destination
    status, data = conn.uid("COPY", uid.encode(), dest_mailbox)
    if status != "OK":
        raise RuntimeError(f"COPY to '{dest_mailbox}' failed: {data}")

    # Mark original as deleted
    conn.uid("STORE", uid.encode(), "+FLAGS", "(\\Deleted)")
    conn.expunge()


def _smtp_send(to: str, subject: str, body: str, cc: str = "",
               bcc: str = "", in_reply_to: str = "", references: str = "",
               html: bool = False):
    """Send an email via SMTP."""
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["From"] = EMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()

    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    if html:
        msg.attach(email.mime.text.MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

    # Build recipient list
    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients.extend(addr.strip() for addr in cc.split(","))
    if bcc:
        recipients.extend(addr.strip() for addr in bcc.split(","))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, recipients, msg.as_string())

    return msg["Message-ID"]


# ---------------------------------------------------------------------------
# Date formatting helpers
# ---------------------------------------------------------------------------

def _format_imap_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to DD-Mon-YYYY for IMAP SINCE/BEFORE criteria."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%d-%b-%Y")


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="proton-email",
    instructions="Email server for Proton Mail Bridge. Access emails via IMAP and send via SMTP.",
)


@mcp.tool(description="List available mailboxes/folders in the email account.")
def list_mailboxes() -> list[dict]:
    """List all available mailboxes/folders."""
    pool = get_pool()
    return pool.execute_with_retry(_imap_list_mailboxes)


@mcp.tool(
    description=(
        "List email metadata with pagination. Returns subject, from, date, uid, "
        "and flags. Supports filtering by mailbox, read/unread status, date range, "
        "sender, and subject keyword. Default: newest first, page_size=20."
    )
)
def list_emails(
    mailbox: str = "INBOX",
    page: int = 1,
    page_size: int = 20,
    unseen_only: bool = False,
    seen_only: bool = False,
    since: Optional[str] = None,
    before: Optional[str] = None,
    from_address: Optional[str] = None,
    subject_search: Optional[str] = None,
) -> dict:
    """List emails with filtering and pagination.

    Args:
        mailbox: Mailbox to list from (default: INBOX)
        page: Page number, 1-indexed (default: 1)
        page_size: Number of emails per page (default: 20)
        unseen_only: Only show unread emails
        seen_only: Only show read emails
        since: Only emails after this date (YYYY-MM-DD)
        before: Only emails before this date (YYYY-MM-DD)
        from_address: Filter by sender address (partial match)
        subject_search: Filter by subject keyword
    """
    # DB-first path
    db = get_db()
    if db.is_synced(mailbox):
        return db.list_emails(
            mailbox, page=page, page_size=page_size,
            unseen_only=unseen_only, seen_only=seen_only,
            since=since, before=before,
            from_address=from_address, subject_search=subject_search,
        )

    # IMAP fallback
    def _do():
        # Build IMAP search criteria
        criteria_parts = []
        if unseen_only:
            criteria_parts.append("UNSEEN")
        if seen_only:
            criteria_parts.append("SEEN")
        if since:
            criteria_parts.append(f'SINCE {_format_imap_date(since)}')
        if before:
            criteria_parts.append(f'BEFORE {_format_imap_date(before)}')
        if from_address:
            criteria_parts.append(f'FROM "{from_address}"')
        if subject_search:
            criteria_parts.append(f'SUBJECT "{subject_search}"')

        if not criteria_parts:
            criteria = "ALL"
        else:
            criteria = " ".join(criteria_parts)

        uids = _imap_search(mailbox, criteria)

        # Proton Bridge assigns lower UIDs to newer emails,
        # so ascending UID order = newest first (no reverse needed).

        total = len(uids)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        page_uids = uids[start:end]

        emails = _imap_fetch_headers(mailbox, page_uids) if page_uids else []

        return {
            "emails": emails,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "mailbox": mailbox,
        }

    pool = get_pool()
    return pool.execute_with_retry(_do)


@mcp.tool(
    description=(
        "Read full email content by UID. Returns subject, from, to, cc, date, "
        "body text, attachments list, and flags. Text/plain is preferred; "
        "text/html is stripped to plain text as fallback."
    )
)
def read_email(uid: str, mailbox: str = "INBOX") -> dict:
    """Read a full email by UID.

    Args:
        uid: The email UID (from list_emails results)
        mailbox: Mailbox containing the email (default: INBOX)
    """
    db = get_db()
    row = db.get_email(mailbox, int(uid))

    if row and row["body_fetched"]:
        # Serve from DB
        attachments = json.loads(row["attachments"]) if row["attachments"] else []
        return {
            "uid": uid,
            "subject": row["subject"] or "",
            "from": row["from_addr"] or "",
            "to": row["to_addr"] or "",
            "cc": row["cc"] or "",
            "bcc": row["bcc"] or "",
            "date": row["date_str"] or "",
            "message_id": row["message_id"] or "",
            "in_reply_to": row["in_reply_to"] or "",
            "references": row["references_"] or "",
            "flags": row["flags"] or "",
            "is_read": bool(row["is_read"]),
            "is_flagged": bool(row["is_flagged"]),
            "body": row["body"] or "",
            "attachments": attachments,
            "source": "local_db",
        }

    if row and not row["body_fetched"]:
        # Header in DB but body not fetched — fetch and cache
        try:
            pool = get_pool()
            syncer = EmailSyncer(db, pool)
            syncer.fetch_single_body(mailbox, int(uid))
            return read_email(uid=uid, mailbox=mailbox)
        except Exception:
            pass  # Fall through to full IMAP fetch

    # Full IMAP fallback
    pool = get_pool()
    result = pool.execute_with_retry(_imap_fetch_full, mailbox, uid)

    # Cache the body in DB if email exists locally
    if row:
        db.update_body(
            mailbox, int(uid), result.get("body", ""),
            json.dumps(result.get("attachments", []))
        )

    return result


@mcp.tool(
    description=(
        "Search emails by keyword using IMAP server-side search. "
        "Searches across subject, from, to, and body. Returns matching email metadata."
    )
)
def search_emails(
    query: str,
    mailbox: str = "INBOX",
    search_in: str = "all",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Search emails by keyword.

    Args:
        query: Search keyword
        mailbox: Mailbox to search (default: INBOX)
        search_in: Where to search - "all", "subject", "from", "to", "body" (default: all)
        page: Page number (default: 1)
        page_size: Results per page (default: 20)
    """
    # FTS5 path for synced mailboxes
    db = get_db()
    if db.is_synced(mailbox) and search_in in ("all", "subject", "from", "to", "body"):
        # Build FTS5 query — map search_in to column prefix
        if search_in == "all":
            fts_query = query
        else:
            col = {"subject": "subject", "from": "from_addr",
                   "to": "to_addr", "body": "body"}[search_in]
            fts_query = f"{col}:{query}"
        try:
            return db.search_fts(fts_query, mailbox=mailbox,
                                 page=page, page_size=page_size)
        except Exception as e:
            logger.warning(f"FTS5 search failed, falling back to IMAP: {e}")

    def _do():
        # Build search criteria based on search_in
        if search_in == "subject":
            criteria = f'SUBJECT "{query}"'
        elif search_in == "from":
            criteria = f'FROM "{query}"'
        elif search_in == "to":
            criteria = f'TO "{query}"'
        elif search_in == "body":
            criteria = f'BODY "{query}"'
        else:
            # Search across multiple fields using OR
            # IMAP OR only takes two operands, so we chain them
            criteria = f'OR OR OR SUBJECT "{query}" FROM "{query}" TO "{query}" BODY "{query}"'

        uids = _imap_search(mailbox, criteria)

        # Proton Bridge workaround: server-side SUBJECT search can miss
        # emails whose subjects contain non-ASCII characters (RFC 2047
        # encoded headers are not decoded before matching). Run a
        # client-side fallback over recent emails to catch misses.
        if search_in in ("subject", "all"):
            server_uid_set = set(uids)
            fallback_uids = _imap_search(mailbox, "ALL")
            # Check up to 200 most recent emails client-side
            candidates = fallback_uids[:200]
            if candidates:
                candidate_headers = _imap_fetch_headers(mailbox, candidates)
                q_lower = query.lower()
                for hdr in candidate_headers:
                    uid_bytes = hdr["uid"].encode()
                    if uid_bytes not in server_uid_set:
                        match = False
                        if search_in in ("subject", "all") and q_lower in hdr.get("subject", "").lower():
                            match = True
                        if search_in == "all" and (
                            q_lower in hdr.get("from", "").lower()
                            or q_lower in hdr.get("to", "").lower()
                        ):
                            match = True
                        if match:
                            uids.append(uid_bytes)

        # Proton Bridge: lower UIDs = newer, already in ascending order = newest first

        total = len(uids)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        page_uids = uids[start:end]

        emails = _imap_fetch_headers(mailbox, page_uids) if page_uids else []

        return {
            "emails": emails,
            "query": query,
            "search_in": search_in,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "mailbox": mailbox,
        }

    pool = get_pool()
    return pool.execute_with_retry(_do)


@mcp.tool(
    description="Send an email via SMTP. Supports plain text and HTML bodies, CC and BCC."
)
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html: bool = False,
) -> dict:
    """Send an email.

    Args:
        to: Recipient email address(es), comma-separated
        subject: Email subject
        body: Email body text
        cc: CC recipients, comma-separated (optional)
        bcc: BCC recipients, comma-separated (optional)
        html: If True, body is treated as HTML (default: False)
    """
    message_id = _smtp_send(to, subject, body, cc=cc, bcc=bcc, html=html)
    return {
        "status": "sent",
        "to": to,
        "subject": subject,
        "message_id": message_id,
    }


@mcp.tool(
    description=(
        "Reply to an email. Automatically sets In-Reply-To and References headers "
        "for proper threading. Prefixes subject with 'Re: ' if not already present."
    )
)
def reply_email(
    uid: str,
    body: str,
    mailbox: str = "INBOX",
    reply_all: bool = False,
    html: bool = False,
) -> dict:
    """Reply to an email.

    Args:
        uid: UID of the email to reply to
        body: Reply body text
        mailbox: Mailbox containing the original email (default: INBOX)
        reply_all: If True, reply to all recipients (default: False)
        html: If True, body is treated as HTML (default: False)
    """
    # Fetch original message for threading headers
    original = read_email(uid=uid, mailbox=mailbox)

    # Build reply subject
    subject = original["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    # Build In-Reply-To and References
    original_message_id = original.get("message_id", "")
    original_references = original.get("references", "")

    in_reply_to = original_message_id
    if original_references:
        references = f"{original_references} {original_message_id}"
    else:
        references = original_message_id

    # Determine recipients
    to = original["from"]
    cc = ""
    if reply_all:
        # Include original To and CC, excluding ourselves
        all_recipients = []
        if original.get("to"):
            all_recipients.extend(
                addr.strip() for addr in original["to"].split(",")
            )
        if original.get("cc"):
            all_recipients.extend(
                addr.strip() for addr in original["cc"].split(",")
            )
        # Remove our own address
        cc_list = [
            addr for addr in all_recipients
            if EMAIL_USER.lower() not in addr.lower()
        ]
        if cc_list:
            cc = ", ".join(cc_list)

    message_id = _smtp_send(
        to, subject, body, cc=cc,
        in_reply_to=in_reply_to, references=references, html=html,
    )

    return {
        "status": "sent",
        "to": to,
        "cc": cc,
        "subject": subject,
        "in_reply_to": in_reply_to,
        "message_id": message_id,
    }


@mcp.tool(
    description=(
        "Move an email between mailboxes. Uses IMAP COPY + DELETE. "
        "Common mailboxes: INBOX, Sent, Drafts, Trash, Archive, Spam."
    )
)
def move_email(uid: str, source_mailbox: str = "INBOX", dest_mailbox: str = "Archive") -> dict:
    """Move an email to a different mailbox.

    Args:
        uid: UID of the email to move
        source_mailbox: Source mailbox (default: INBOX)
        dest_mailbox: Destination mailbox (default: Archive)
    """
    pool = get_pool()
    pool.execute_with_retry(_imap_move, uid, source_mailbox, dest_mailbox)

    # Remove from source in local DB
    db = get_db()
    db.delete_uids(source_mailbox, {int(uid)})

    return {
        "status": "moved",
        "uid": uid,
        "from_mailbox": source_mailbox,
        "to_mailbox": dest_mailbox,
    }


@mcp.tool(
    description=(
        "Mark an email as read, unread, flagged, or unflagged. "
        "Supports actions: read, unread, flagged, unflagged."
    )
)
def mark_email(uid: str, action: str, mailbox: str = "INBOX") -> dict:
    """Mark an email with a flag action.

    Args:
        uid: UID of the email
        action: One of "read", "unread", "flagged", "unflagged"
        mailbox: Mailbox containing the email (default: INBOX)
    """
    valid_actions = {"read", "unread", "flagged", "unflagged"}
    if action not in valid_actions:
        raise ValueError(f"Invalid action '{action}'. Must be one of: {valid_actions}")

    pool = get_pool()

    if action == "read":
        pool.execute_with_retry(_imap_store_flags, mailbox, uid, "(\\Seen)", "+FLAGS")
    elif action == "unread":
        pool.execute_with_retry(_imap_store_flags, mailbox, uid, "(\\Seen)", "-FLAGS")
    elif action == "flagged":
        pool.execute_with_retry(_imap_store_flags, mailbox, uid, "(\\Flagged)", "+FLAGS")
    elif action == "unflagged":
        pool.execute_with_retry(_imap_store_flags, mailbox, uid, "(\\Flagged)", "-FLAGS")

    # Update local DB
    db = get_db()
    row = db.get_email(mailbox, int(uid))
    if row:
        flags = row["flags"] or ""
        if action == "read":
            if "\\Seen" not in flags:
                flags = (flags + " \\Seen").strip()
            db.update_flags(mailbox, int(uid), flags, True, bool(row["is_flagged"]))
        elif action == "unread":
            flags = flags.replace("\\Seen", "").strip()
            db.update_flags(mailbox, int(uid), flags, False, bool(row["is_flagged"]))
        elif action == "flagged":
            if "\\Flagged" not in flags:
                flags = (flags + " \\Flagged").strip()
            db.update_flags(mailbox, int(uid), flags, bool(row["is_read"]), True)
        elif action == "unflagged":
            flags = flags.replace("\\Flagged", "").strip()
            db.update_flags(mailbox, int(uid), flags, bool(row["is_read"]), False)

    return {
        "status": "updated",
        "uid": uid,
        "action": action,
        "mailbox": mailbox,
    }


@mcp.tool(
    description="Delete an email by moving it to the Trash folder."
)
def delete_email(uid: str, mailbox: str = "INBOX") -> dict:
    """Delete an email (move to Trash).

    Args:
        uid: UID of the email to delete
        mailbox: Mailbox containing the email (default: INBOX)
    """
    pool = get_pool()
    # Proton Mail Bridge uses "Trash" as the trash folder
    pool.execute_with_retry(_imap_move, uid, mailbox, "Trash")

    # Remove from local DB
    db = get_db()
    db.delete_uids(mailbox, {int(uid)})

    return {
        "status": "deleted",
        "uid": uid,
        "from_mailbox": mailbox,
        "moved_to": "Trash",
    }


def _imap_download_attachment(mailbox: str, uid: str, filename: str) -> dict:
    """Download a specific attachment from an email by UID and filename."""
    pool = get_pool()
    conn = pool.select_mailbox(mailbox, readonly=True)

    status, data = conn.uid("FETCH", uid.encode(), "(RFC822)")
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")

    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            msg = email.message_from_bytes(item[1])

            # Extract email metadata
            subject = decode_header_value(msg.get("Subject"))
            from_addr = decode_header_value(msg.get("From"))

            if msg.is_multipart():
                for part in msg.walk():
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" not in disposition:
                        continue
                    part_filename = part.get_filename()
                    if part_filename:
                        part_filename = _sanitize_filename(decode_header_value(part_filename))
                    if part_filename == filename:
                        payload = part.get_payload(decode=True)
                        if payload is None:
                            raise RuntimeError(f"Attachment '{filename}' has no content")

                        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
                        # Avoid overwriting: add suffix if file exists
                        save_path = os.path.join(ATTACHMENTS_DIR, filename)
                        base, ext = os.path.splitext(filename)
                        counter = 1
                        while os.path.exists(save_path):
                            save_path = os.path.join(ATTACHMENTS_DIR, f"{base}_{counter}{ext}")
                            counter += 1

                        with open(save_path, "wb") as f:
                            f.write(payload)

                        content_type = part.get_content_type()
                        size_bytes = len(payload)

                        # Track in DB
                        db = get_db()
                        db.add_download({
                            "email_uid": int(uid),
                            "mailbox": mailbox,
                            "filename": filename,
                            "saved_as": os.path.basename(save_path),
                            "file_path": save_path,
                            "size_bytes": size_bytes,
                            "content_type": content_type,
                            "email_subject": subject,
                            "email_from": from_addr,
                            "downloaded_at": datetime.now(timezone.utc).isoformat(),
                        })

                        return {
                            "filename": filename,
                            "saved_to": save_path,
                            "size_bytes": size_bytes,
                            "content_type": content_type,
                        }

    raise RuntimeError(f"Attachment '{filename}' not found in email UID {uid}")


@mcp.tool(
    description=(
        "Download an attachment from an email. Saves to the project downloads folder. "
        "Use read_email first to see available attachments and their filenames."
    )
)
def download_attachment(
    uid: str,
    filename: str,
    mailbox: str = "INBOX",
) -> dict:
    """Download an email attachment.

    Args:
        uid: UID of the email containing the attachment
        filename: Exact filename of the attachment (from read_email results)
        mailbox: Mailbox containing the email (default: INBOX)
    """
    pool = get_pool()
    return pool.execute_with_retry(_imap_download_attachment, mailbox, uid, filename)


@mcp.tool(
    description=(
        "List all downloaded attachments from the index. Shows filename, email subject, "
        "sender, date, and file path. Optionally filter by keyword."
    )
)
def list_downloads(query: Optional[str] = None) -> dict:
    """List downloaded attachments.

    Args:
        query: Optional keyword to filter by filename or email subject
    """
    db = get_db()
    downloads = db.list_downloads(query)
    return {
        "total": len(downloads),
        "downloads": downloads,
    }


# ---------------------------------------------------------------------------
# New tools: sync & local search
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Sync emails from IMAP into local SQLite database for offline access. "
        "First sync fetches all headers (~2-3 min for 13k emails). "
        "Subsequent syncs are incremental (seconds). "
        "Use include_bodies=True to also fetch and cache email bodies."
    )
)
def sync_emails(
    mailbox: str = "INBOX",
    all_mailboxes: bool = False,
    include_bodies: bool = False,
) -> dict:
    """Sync emails from IMAP to local database.

    Args:
        mailbox: Mailbox to sync (default: INBOX)
        all_mailboxes: If True, sync INBOX, Sent, Starred, Archive, Drafts
        include_bodies: If True, also fetch email bodies (slow for first sync)
    """
    pool = get_pool()
    db = get_db()
    syncer = EmailSyncer(db, pool)

    mailboxes = SYNC_MAILBOXES if all_mailboxes else [mailbox]
    results = []

    for mbox in mailboxes:
        try:
            summary = pool.execute_with_retry(syncer.sync_mailbox, mbox, include_bodies)
            results.append(summary)
        except Exception as e:
            results.append({"mailbox": mbox, "error": str(e)})

    return {"synced": results}


@mcp.tool(
    description=(
        "Show sync status for all mailboxes. Reports last sync time, "
        "total emails synced, and how many bodies have been fetched."
    )
)
def sync_status() -> dict:
    """Get sync status for all mailboxes."""
    db = get_db()
    states = db.get_all_sync_states()
    if not states:
        return {"status": "No mailboxes synced yet. Run sync_emails first."}
    return {"mailboxes": states}


@mcp.tool(
    description=(
        "Search emails locally using FTS5 full-text search. "
        "Much faster than IMAP search. Supports: exact phrases with quotes, "
        "prefix matching with *, AND/OR/NOT operators. "
        "Requires sync_emails to have been run first."
    )
)
def local_search(
    query: str,
    mailbox: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Search emails locally using full-text search.

    Args:
        query: FTS5 search query. Examples: '"exact phrase"', 'word1 AND word2', 'prefix*'
        mailbox: Limit search to specific mailbox (default: search all synced mailboxes)
        page: Page number (default: 1)
        page_size: Results per page (default: 20)
    """
    db = get_db()
    return db.search_fts(query, mailbox=mailbox, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
