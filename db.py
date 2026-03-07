"""
Local email database with SQLite + FTS5.

Provides offline access to email headers and cached bodies,
with full-text search via FTS5. Bridge is only needed for
sending, replying, and syncing new mail.
"""

import sqlite3
import os
import json
import logging
import imaplib
import email
import email.utils
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("email-db")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "emails.db")
ATTACHMENTS_DIR = os.path.join(DB_DIR, "attachments")

# Mailboxes to sync (skip Trash, Spam, All Mail)
SYNC_MAILBOXES = ["INBOX", "Sent", "Starred", "Archive", "Drafts"]

# Batch sizes
HEADER_BATCH_SIZE = 200
BODY_BATCH_SIZE = 25


# ---------------------------------------------------------------------------
# EmailDB
# ---------------------------------------------------------------------------

class EmailDB:
    """SQLite database for local email storage with FTS5 search."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        return self._conn

    def _init_schema(self):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sync_state (
                mailbox TEXT PRIMARY KEY,
                last_sync_at TEXT,
                uid_validity INTEGER,
                total_synced INTEGER DEFAULT 0,
                last_uid_seen INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                mailbox TEXT NOT NULL,
                message_id TEXT,
                in_reply_to TEXT,
                references_ TEXT,
                subject TEXT,
                from_addr TEXT,
                to_addr TEXT,
                cc TEXT,
                bcc TEXT,
                date_str TEXT,
                date_parsed TEXT,
                flags TEXT,
                is_read INTEGER DEFAULT 0,
                is_flagged INTEGER DEFAULT 0,
                body TEXT,
                body_fetched INTEGER DEFAULT 0,
                attachments TEXT,
                raw_size INTEGER,
                UNIQUE(uid, mailbox)
            );

            CREATE INDEX IF NOT EXISTS idx_emails_mailbox ON emails(mailbox);
            CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_parsed);
            CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);

            CREATE TABLE IF NOT EXISTS attachment_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_uid INTEGER,
                mailbox TEXT,
                filename TEXT,
                saved_as TEXT,
                file_path TEXT,
                size_bytes INTEGER,
                content_type TEXT,
                email_subject TEXT,
                email_from TEXT,
                downloaded_at TEXT
            );
        """)

        # Create FTS5 virtual table if not exists
        # Check if it exists first to avoid error
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emails_fts'"
        ).fetchone()
        if not row:
            c.execute("""
                CREATE VIRTUAL TABLE emails_fts USING fts5(
                    subject, from_addr, to_addr, body,
                    content='emails',
                    content_rowid='id'
                )
            """)
            # Triggers to keep FTS in sync
            c.executescript("""
                CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
                    INSERT INTO emails_fts(rowid, subject, from_addr, to_addr, body)
                    VALUES (new.id, new.subject, new.from_addr, new.to_addr, new.body);
                END;

                CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
                    INSERT INTO emails_fts(emails_fts, rowid, subject, from_addr, to_addr, body)
                    VALUES ('delete', old.id, old.subject, old.from_addr, old.to_addr, old.body);
                END;

                CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
                    INSERT INTO emails_fts(emails_fts, rowid, subject, from_addr, to_addr, body)
                    VALUES ('delete', old.id, old.subject, old.from_addr, old.to_addr, old.body);
                    INSERT INTO emails_fts(rowid, subject, from_addr, to_addr, body)
                    VALUES (new.id, new.subject, new.from_addr, new.to_addr, new.body);
                END;
            """)
        c.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- Sync state --

    def get_sync_state(self, mailbox: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        return dict(row) if row else None

    def update_sync_state(self, mailbox: str, uid_validity: int,
                          total_synced: int, last_uid_seen: int):
        self.conn.execute("""
            INSERT INTO sync_state (mailbox, last_sync_at, uid_validity, total_synced, last_uid_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mailbox) DO UPDATE SET
                last_sync_at = excluded.last_sync_at,
                uid_validity = excluded.uid_validity,
                total_synced = excluded.total_synced,
                last_uid_seen = excluded.last_uid_seen
        """, (mailbox, datetime.now(timezone.utc).isoformat(), uid_validity,
              total_synced, last_uid_seen))
        self.conn.commit()

    def get_all_sync_states(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM sync_state").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            bodies = self.conn.execute(
                "SELECT COUNT(*) FROM emails WHERE mailbox = ? AND body_fetched = 1",
                (d["mailbox"],)
            ).fetchone()[0]
            d["bodies_fetched"] = bodies
            result.append(d)
        return result

    # -- Email CRUD --

    def get_local_uids(self, mailbox: str) -> set[int]:
        rows = self.conn.execute(
            "SELECT uid FROM emails WHERE mailbox = ?", (mailbox,)
        ).fetchall()
        return {row[0] for row in rows}

    def upsert_emails(self, emails_data: list[dict]):
        """Batch upsert email headers."""
        if not emails_data:
            return
        self.conn.executemany("""
            INSERT INTO emails (uid, mailbox, message_id, in_reply_to, references_,
                subject, from_addr, to_addr, cc, bcc, date_str, date_parsed,
                flags, is_read, is_flagged, attachments, raw_size, body, body_fetched)
            VALUES (:uid, :mailbox, :message_id, :in_reply_to, :references_,
                :subject, :from_addr, :to_addr, :cc, :bcc, :date_str, :date_parsed,
                :flags, :is_read, :is_flagged, :attachments, :raw_size, :body, :body_fetched)
            ON CONFLICT(uid, mailbox) DO UPDATE SET
                message_id = excluded.message_id,
                in_reply_to = excluded.in_reply_to,
                references_ = excluded.references_,
                subject = excluded.subject,
                from_addr = excluded.from_addr,
                to_addr = excluded.to_addr,
                cc = excluded.cc,
                bcc = excluded.bcc,
                date_str = excluded.date_str,
                date_parsed = excluded.date_parsed,
                flags = excluded.flags,
                is_read = excluded.is_read,
                is_flagged = excluded.is_flagged,
                attachments = excluded.attachments,
                raw_size = excluded.raw_size
        """, emails_data)
        self.conn.commit()

    def update_flags(self, mailbox: str, uid: int, flags: str,
                     is_read: bool, is_flagged: bool):
        self.conn.execute("""
            UPDATE emails SET flags = ?, is_read = ?, is_flagged = ?
            WHERE uid = ? AND mailbox = ?
        """, (flags, int(is_read), int(is_flagged), uid, mailbox))
        self.conn.commit()

    def update_body(self, mailbox: str, uid: int, body: str, attachments_json: str):
        self.conn.execute("""
            UPDATE emails SET body = ?, body_fetched = 1, attachments = ?
            WHERE uid = ? AND mailbox = ?
        """, (body, attachments_json, uid, mailbox))
        self.conn.commit()

    def delete_uids(self, mailbox: str, uids: set[int]):
        if not uids:
            return
        placeholders = ",".join("?" * len(uids))
        self.conn.execute(
            f"DELETE FROM emails WHERE mailbox = ? AND uid IN ({placeholders})",
            [mailbox] + list(uids)
        )
        self.conn.commit()

    def get_email(self, mailbox: str, uid: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM emails WHERE uid = ? AND mailbox = ?", (uid, mailbox)
        ).fetchone()
        return dict(row) if row else None

    def list_emails(self, mailbox: str, page: int = 1, page_size: int = 20,
                    unseen_only: bool = False, seen_only: bool = False,
                    since: Optional[str] = None, before: Optional[str] = None,
                    from_address: Optional[str] = None,
                    subject_search: Optional[str] = None) -> dict:
        """List emails from local DB with filtering and pagination."""
        conditions = ["mailbox = ?"]
        params: list = [mailbox]

        if unseen_only:
            conditions.append("is_read = 0")
        if seen_only:
            conditions.append("is_read = 1")
        if since:
            conditions.append("date_parsed >= ?")
            params.append(since)
        if before:
            conditions.append("date_parsed < ?")
            params.append(before)
        if from_address:
            conditions.append("from_addr LIKE ?")
            params.append(f"%{from_address}%")
        if subject_search:
            conditions.append("subject LIKE ?")
            params.append(f"%{subject_search}%")

        where = " AND ".join(conditions)

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM emails WHERE {where}", params
        ).fetchone()[0]

        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page - 1) * page_size

        # Proton Bridge: lower UIDs = newer, so ORDER BY uid ASC = newest first
        rows = self.conn.execute(
            f"""SELECT uid, subject, from_addr, to_addr, date_str, message_id,
                       flags, is_read, is_flagged
                FROM emails WHERE {where}
                ORDER BY uid ASC
                LIMIT ? OFFSET ?""",
            params + [page_size, offset]
        ).fetchall()

        emails = []
        for row in rows:
            emails.append({
                "uid": str(row["uid"]),
                "subject": row["subject"] or "",
                "from": row["from_addr"] or "",
                "to": row["to_addr"] or "",
                "date": row["date_str"] or "",
                "message_id": row["message_id"] or "",
                "flags": row["flags"] or "",
                "is_read": bool(row["is_read"]),
                "is_flagged": bool(row["is_flagged"]),
            })

        return {
            "emails": emails,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "mailbox": mailbox,
            "source": "local_db",
        }

    def search_fts(self, query: str, mailbox: Optional[str] = None,
                   page: int = 1, page_size: int = 20) -> dict:
        """Full-text search using FTS5."""
        if mailbox:
            count_sql = """
                SELECT COUNT(*) FROM emails_fts
                JOIN emails ON emails.id = emails_fts.rowid
                WHERE emails_fts MATCH ? AND emails.mailbox = ?
            """
            search_sql = """
                SELECT emails.uid, emails.subject, emails.from_addr, emails.to_addr,
                       emails.date_str, emails.message_id, emails.flags,
                       emails.is_read, emails.is_flagged, emails.mailbox,
                       rank
                FROM emails_fts
                JOIN emails ON emails.id = emails_fts.rowid
                WHERE emails_fts MATCH ? AND emails.mailbox = ?
                ORDER BY rank
                LIMIT ? OFFSET ?
            """
            count_params = [query, mailbox]
            search_params = [query, mailbox, page_size, (page - 1) * page_size]
        else:
            count_sql = """
                SELECT COUNT(*) FROM emails_fts
                JOIN emails ON emails.id = emails_fts.rowid
                WHERE emails_fts MATCH ?
            """
            search_sql = """
                SELECT emails.uid, emails.subject, emails.from_addr, emails.to_addr,
                       emails.date_str, emails.message_id, emails.flags,
                       emails.is_read, emails.is_flagged, emails.mailbox,
                       rank
                FROM emails_fts
                JOIN emails ON emails.id = emails_fts.rowid
                WHERE emails_fts MATCH ?
                ORDER BY rank
                LIMIT ? OFFSET ?
            """
            count_params = [query]
            search_params = [query, page_size, (page - 1) * page_size]

        total = self.conn.execute(count_sql, count_params).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)

        rows = self.conn.execute(search_sql, search_params).fetchall()

        emails = []
        for row in rows:
            emails.append({
                "uid": str(row["uid"]),
                "subject": row["subject"] or "",
                "from": row["from_addr"] or "",
                "to": row["to_addr"] or "",
                "date": row["date_str"] or "",
                "message_id": row["message_id"] or "",
                "flags": row["flags"] or "",
                "is_read": bool(row["is_read"]),
                "is_flagged": bool(row["is_flagged"]),
                "mailbox": row["mailbox"],
            })

        return {
            "emails": emails,
            "query": query,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "mailbox": mailbox or "all",
            "source": "fts5",
        }

    def is_synced(self, mailbox: str) -> bool:
        state = self.get_sync_state(mailbox)
        return state is not None and state["total_synced"] > 0

    # -- Attachment downloads --

    def add_download(self, entry: dict):
        self.conn.execute("""
            INSERT INTO attachment_downloads
                (email_uid, mailbox, filename, saved_as, file_path,
                 size_bytes, content_type, email_subject, email_from, downloaded_at)
            VALUES (:email_uid, :mailbox, :filename, :saved_as, :file_path,
                    :size_bytes, :content_type, :email_subject, :email_from, :downloaded_at)
        """, entry)
        self.conn.commit()

    def list_downloads(self, query: Optional[str] = None) -> list[dict]:
        if query:
            q = f"%{query}%"
            rows = self.conn.execute("""
                SELECT * FROM attachment_downloads
                WHERE filename LIKE ? OR email_subject LIKE ? OR email_from LIKE ?
                ORDER BY id DESC
            """, (q, q, q)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM attachment_downloads ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# EmailSyncer
# ---------------------------------------------------------------------------

def _parse_date(date_str: Optional[str]) -> Optional[str]:
    """Parse email date to ISO 8601, return None on failure."""
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        return None


def _decode_header_value(value: Optional[str]) -> str:
    """Decode RFC 2047 header."""
    if value is None:
        return ""
    parts = []
    for decoded_bytes, charset in email.header.decode_header(value):
        if isinstance(decoded_bytes, bytes):
            enc = charset or "utf-8"
            try:
                parts.append(decoded_bytes.decode(enc, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(decoded_bytes.decode("utf-8", errors="replace"))
        else:
            parts.append(str(decoded_bytes))
    return "".join(parts)


class EmailSyncer:
    """Syncs email headers from IMAP into the local SQLite database."""

    def __init__(self, db: EmailDB, pool):
        """
        Args:
            db: EmailDB instance
            pool: IMAPConnectionPool from server.py
        """
        self.db = db
        self.pool = pool

    def sync_mailbox(self, mailbox: str, include_bodies: bool = False) -> dict:
        """Sync a single mailbox. Returns summary stats."""
        conn = self.pool.select_mailbox(mailbox, readonly=True)

        # Get UIDVALIDITY
        status, data = conn.status(mailbox, "(UIDVALIDITY)")
        uid_validity = 0
        if status == "OK" and data:
            m = re.search(r"UIDVALIDITY\s+(\d+)", data[0].decode("utf-8", errors="replace"))
            if m:
                uid_validity = int(m.group(1))

        # Check if UIDVALIDITY changed (need full re-sync)
        state = self.db.get_sync_state(mailbox)
        if state and state["uid_validity"] and state["uid_validity"] != uid_validity:
            logger.warning(f"UIDVALIDITY changed for {mailbox}, doing full re-sync")
            self.db.delete_uids(mailbox, self.db.get_local_uids(mailbox))

        # Get all server UIDs
        conn = self.pool.select_mailbox(mailbox, readonly=True)
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"SEARCH ALL failed for {mailbox}: {data}")
        server_uids: set[int] = set()
        if data and data[0]:
            server_uids = {int(u) for u in data[0].split()}

        local_uids = self.db.get_local_uids(mailbox)

        new_uids = server_uids - local_uids
        deleted_uids = local_uids - server_uids

        # Remove deleted
        if deleted_uids:
            self.db.delete_uids(mailbox, deleted_uids)

        # Fetch headers for new UIDs in batches
        new_uid_list = sorted(new_uids)
        fetched = 0
        for i in range(0, len(new_uid_list), HEADER_BATCH_SIZE):
            batch = new_uid_list[i:i + HEADER_BATCH_SIZE]
            headers = self._fetch_headers_batch(conn, mailbox, batch)
            self.db.upsert_emails(headers)
            fetched += len(headers)
            if (i + HEADER_BATCH_SIZE) < len(new_uid_list):
                # Re-select to keep connection alive on long syncs
                conn = self.pool.select_mailbox(mailbox, readonly=True)

        # Update flags for existing UIDs (batch)
        existing_uids = local_uids & server_uids
        flags_updated = 0
        if existing_uids:
            flags_updated = self._sync_flags(conn, mailbox, existing_uids)

        # Optional body sync
        bodies_fetched = 0
        if include_bodies:
            bodies_fetched = self._sync_bodies(mailbox)

        total_local = len(self.db.get_local_uids(mailbox))
        last_uid = max(server_uids) if server_uids else 0
        self.db.update_sync_state(mailbox, uid_validity, total_local, last_uid)

        return {
            "mailbox": mailbox,
            "server_total": len(server_uids),
            "new_fetched": fetched,
            "deleted_removed": len(deleted_uids),
            "flags_updated": flags_updated,
            "bodies_fetched": bodies_fetched,
            "local_total": total_local,
        }

    def _fetch_headers_batch(self, conn, mailbox: str,
                             uids: list[int]) -> list[dict]:
        """Fetch extended headers for a batch of UIDs."""
        if not uids:
            return []

        # Increase max line for large batch fetches
        old_maxline = imaplib._MAXLINE
        imaplib._MAXLINE = 10_000_000

        try:
            uid_str = ",".join(str(u) for u in uids).encode()
            fetch_items = (
                "(UID FLAGS RFC822.SIZE "
                "BODY.PEEK[HEADER.FIELDS "
                "(SUBJECT FROM TO CC BCC DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])"
            )
            status, data = conn.uid("FETCH", uid_str, fetch_items)
            if status != "OK":
                raise RuntimeError(f"FETCH headers failed: {data}")

            results = []
            i = 0
            while i < len(data):
                item = data[i]
                if isinstance(item, tuple) and len(item) == 2:
                    meta_line = (item[0].decode("utf-8", errors="replace")
                                 if isinstance(item[0], bytes) else str(item[0]))
                    header_bytes = item[1]

                    uid_match = re.search(r"UID\s+(\d+)", meta_line)
                    uid_val = int(uid_match.group(1)) if uid_match else 0

                    flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", meta_line)
                    flags = flags_match.group(1) if flags_match else ""

                    size_match = re.search(r"RFC822\.SIZE\s+(\d+)", meta_line)
                    raw_size = int(size_match.group(1)) if size_match else 0

                    msg = email.message_from_bytes(header_bytes)
                    subject = _decode_header_value(msg.get("Subject"))
                    from_addr = _decode_header_value(msg.get("From"))
                    to_addr = _decode_header_value(msg.get("To"))
                    cc = _decode_header_value(msg.get("Cc"))
                    bcc = _decode_header_value(msg.get("Bcc"))
                    date_str = msg.get("Date", "")
                    message_id = msg.get("Message-ID", "")
                    in_reply_to = msg.get("In-Reply-To", "")
                    references = msg.get("References", "")

                    results.append({
                        "uid": uid_val,
                        "mailbox": mailbox,
                        "message_id": message_id,
                        "in_reply_to": in_reply_to,
                        "references_": references,
                        "subject": subject,
                        "from_addr": from_addr,
                        "to_addr": to_addr,
                        "cc": cc,
                        "bcc": bcc,
                        "date_str": date_str,
                        "date_parsed": _parse_date(date_str),
                        "flags": flags,
                        "is_read": 1 if "\\Seen" in flags else 0,
                        "is_flagged": 1 if "\\Flagged" in flags else 0,
                        "attachments": None,
                        "raw_size": raw_size,
                        "body": None,
                        "body_fetched": 0,
                    })
                    i += 2
                else:
                    i += 1

            return results
        finally:
            imaplib._MAXLINE = old_maxline

    def _sync_flags(self, conn, mailbox: str, uids: set[int]) -> int:
        """Batch fetch and update flags for existing UIDs."""
        old_maxline = imaplib._MAXLINE
        imaplib._MAXLINE = 10_000_000
        updated = 0

        try:
            uid_list = sorted(uids)
            for i in range(0, len(uid_list), HEADER_BATCH_SIZE):
                batch = uid_list[i:i + HEADER_BATCH_SIZE]
                uid_str = ",".join(str(u) for u in batch).encode()

                status, data = conn.uid("FETCH", uid_str, "(UID FLAGS)")
                if status != "OK":
                    continue

                j = 0
                while j < len(data):
                    item = data[j]
                    raw = (item[0] if isinstance(item, tuple) else item)
                    if raw is None:
                        j += 1
                        continue
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

                    uid_match = re.search(r"UID\s+(\d+)", line)
                    flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", line)
                    if uid_match and flags_match:
                        uid_val = int(uid_match.group(1))
                        flags = flags_match.group(1)
                        is_read = "\\Seen" in flags
                        is_flagged = "\\Flagged" in flags

                        existing = self.db.get_email(mailbox, uid_val)
                        if existing and existing["flags"] != flags:
                            self.db.update_flags(mailbox, uid_val, flags, is_read, is_flagged)
                            updated += 1

                    j += (2 if isinstance(item, tuple) else 1)

                if (i + HEADER_BATCH_SIZE) < len(uid_list):
                    conn = self.pool.select_mailbox(mailbox, readonly=True)

        finally:
            imaplib._MAXLINE = old_maxline

        return updated

    def _sync_bodies(self, mailbox: str) -> int:
        """Fetch bodies for emails that don't have them yet."""
        rows = self.db.conn.execute(
            "SELECT uid FROM emails WHERE mailbox = ? AND body_fetched = 0",
            (mailbox,)
        ).fetchall()

        uid_list = [row[0] for row in rows]
        fetched = 0

        for i in range(0, len(uid_list), BODY_BATCH_SIZE):
            batch = uid_list[i:i + BODY_BATCH_SIZE]
            for uid in batch:
                try:
                    self._fetch_and_cache_body(mailbox, uid)
                    fetched += 1
                except Exception as e:
                    logger.warning(f"Failed to fetch body for UID {uid}: {e}")

        return fetched

    def _fetch_and_cache_body(self, mailbox: str, uid: int):
        """Fetch a single email body from IMAP and cache it in DB."""
        # Import here to avoid circular — uses server.py's extract_body/list_attachments
        from server import extract_body, list_attachments

        conn = self.pool.select_mailbox(mailbox, readonly=True)
        status, data = conn.uid("FETCH", str(uid).encode(), "(RFC822)")
        if status != "OK":
            raise RuntimeError(f"FETCH body failed for UID {uid}")

        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                msg = email.message_from_bytes(item[1])
                body = extract_body(msg)
                attachments = list_attachments(msg)
                self.db.update_body(mailbox, uid, body, json.dumps(attachments))
                return

        raise RuntimeError(f"Body not found for UID {uid}")

    def fetch_single_body(self, mailbox: str, uid: int) -> dict:
        """Fetch body for a single email, cache it, return full email dict."""
        self._fetch_and_cache_body(mailbox, uid)
        return self.db.get_email(mailbox, uid)
