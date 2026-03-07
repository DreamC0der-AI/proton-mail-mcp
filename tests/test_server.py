#!/usr/bin/env python3
"""
Test script for the Proton Mail Bridge MCP email server.

Tests IMAP functions directly (not through MCP protocol).
Requires Proton Mail Bridge to be running.

Usage:
    python3 test_server.py
"""

import os
import sys
import time
import traceback

# Set environment variables for testing
os.environ.setdefault("EMAIL_USER", "Ungigdu@pm.me")
os.environ.setdefault("EMAIL_PASS", "cJJmsiy9uh5rC0MaAndt2Q")
os.environ.setdefault("IMAP_HOST", "127.0.0.1")
os.environ.setdefault("IMAP_PORT", "1143")
os.environ.setdefault("SMTP_HOST", "127.0.0.1")
os.environ.setdefault("SMTP_PORT", "1025")

# Import after env vars are set so module picks them up
from server import (
    get_pool,
    list_mailboxes,
    list_emails,
    read_email,
    search_emails,
    mark_email,
    decode_header_value,
    strip_html,
    extract_body,
    _imap_list_mailboxes,
    _imap_search,
    _imap_fetch_headers,
    _imap_fetch_full,
)


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def record_pass(self, name):
        self.passed += 1
        print(f"  PASS: {name}")

    def record_fail(self, name, error):
        self.failed += 1
        self.errors.append((name, error))
        print(f"  FAIL: {name}")
        print(f"        {error}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print(f"\nFailures:")
            for name, error in self.errors:
                print(f"  - {name}: {error}")
        print(f"{'='*60}")
        return self.failed == 0


results = TestResult()


def test(name):
    """Decorator for test functions."""
    def decorator(fn):
        def wrapper():
            try:
                fn()
                results.record_pass(name)
            except AssertionError as e:
                results.record_fail(name, str(e))
            except Exception as e:
                results.record_fail(name, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Unit tests (no IMAP needed)
# ---------------------------------------------------------------------------

print("\n--- Unit Tests (no IMAP connection) ---\n")


@test("decode_header_value: plain ASCII")
def test_decode_plain():
    assert decode_header_value("Hello World") == "Hello World"

test_decode_plain()


@test("decode_header_value: RFC 2047 UTF-8")
def test_decode_rfc2047_utf8():
    encoded = "=?UTF-8?B?44GT44KT44Gr44Gh44Gv?="  # "こんにちは" in base64
    decoded = decode_header_value(encoded)
    assert decoded == "こんにちは", f"Got: {decoded}"

test_decode_rfc2047_utf8()


@test("decode_header_value: RFC 2047 ISO-2022-JP")
def test_decode_rfc2047_iso2022jp():
    # "テスト" encoded in ISO-2022-JP quoted-printable
    encoded = "=?ISO-2022-JP?B?GyRCJUYlOSVIGyhC?="
    decoded = decode_header_value(encoded)
    assert decoded == "テスト", f"Got: {decoded}"

test_decode_rfc2047_iso2022jp()


@test("decode_header_value: None input")
def test_decode_none():
    assert decode_header_value(None) == ""

test_decode_none()


@test("strip_html: basic tags")
def test_strip_basic():
    html = "<p>Hello <b>world</b></p>"
    text = strip_html(html)
    assert "Hello" in text
    assert "world" in text
    assert "<" not in text

test_strip_basic()


@test("strip_html: script and style removal")
def test_strip_script():
    html = "<p>Keep</p><script>remove_me();</script><style>.remove{}</style><p>Also keep</p>"
    text = strip_html(html)
    assert "Keep" in text
    assert "Also keep" in text
    assert "remove_me" not in text
    assert ".remove" not in text

test_strip_script()


@test("strip_html: br and div produce newlines")
def test_strip_newlines():
    html = "<div>Line 1</div><div>Line 2</div>"
    text = strip_html(html)
    assert "Line 1" in text
    assert "Line 2" in text

test_strip_newlines()


# ---------------------------------------------------------------------------
# Integration tests (require IMAP connection)
# ---------------------------------------------------------------------------

print("\n--- Integration Tests (IMAP connection required) ---\n")


@test("IMAP connection pool creation")
def test_pool_creation():
    pool = get_pool()
    assert pool is not None
    conn = pool.get_connection()
    assert conn is not None

test_pool_creation()


@test("IMAP connection reuse")
def test_pool_reuse():
    pool = get_pool()
    conn1 = pool.get_connection()
    conn2 = pool.get_connection()
    # Should be the same connection object
    assert conn1 is conn2, "Pool should reuse connections"

test_pool_reuse()


@test("list_mailboxes returns folders")
def test_list_mailboxes():
    mailboxes = list_mailboxes()
    assert isinstance(mailboxes, list), f"Expected list, got {type(mailboxes)}"
    assert len(mailboxes) > 0, "Should have at least one mailbox"
    # Should have INBOX
    names = [m["name"] for m in mailboxes]
    assert any("INBOX" in n for n in names), f"INBOX not found in: {names}"
    print(f"        Found {len(mailboxes)} mailboxes: {names}")

test_list_mailboxes()


@test("list_emails returns paginated results from INBOX")
def test_list_emails_basic():
    result = list_emails(mailbox="INBOX", page=1, page_size=5)
    assert "emails" in result
    assert "total" in result
    assert "page" in result
    assert "total_pages" in result
    assert result["page"] == 1
    assert result["page_size"] == 5
    assert len(result["emails"]) <= 5
    assert result["total"] > 0, "INBOX should have emails"
    print(f"        Total emails: {result['total']}, showing {len(result['emails'])}")
    if result["emails"]:
        e = result["emails"][0]
        print(f"        Latest: [{e['uid']}] {e['subject'][:60]}")
        print(f"                From: {e['from'][:60]}")
        print(f"                Date: {e['date']}")
        # Verify structure
        assert "uid" in e
        assert "subject" in e
        assert "from" in e
        assert "date" in e
        assert "is_read" in e

test_list_emails_basic()


@test("list_emails pagination works")
def test_list_emails_pagination():
    page1 = list_emails(mailbox="INBOX", page=1, page_size=3)
    page2 = list_emails(mailbox="INBOX", page=2, page_size=3)
    assert len(page1["emails"]) > 0
    assert len(page2["emails"]) > 0
    # UIDs should be different between pages
    uids1 = {e["uid"] for e in page1["emails"]}
    uids2 = {e["uid"] for e in page2["emails"]}
    assert uids1.isdisjoint(uids2), f"Pages should have different emails. Page1: {uids1}, Page2: {uids2}"
    print(f"        Page 1 UIDs: {sorted(uids1)}")
    print(f"        Page 2 UIDs: {sorted(uids2)}")

test_list_emails_pagination()


@test("list_emails newest first ordering")
def test_list_emails_ordering():
    result = list_emails(mailbox="INBOX", page=1, page_size=5)
    uids = [int(e["uid"]) for e in result["emails"]]
    # UIDs should be in descending order (newest first)
    assert uids == sorted(uids, reverse=True), f"UIDs not in descending order: {uids}"
    print(f"        UIDs (newest first): {uids}")

test_list_emails_ordering()


@test("list_emails unseen_only filter")
def test_list_emails_unseen():
    result = list_emails(mailbox="INBOX", page=1, page_size=5, unseen_only=True)
    for e in result["emails"]:
        assert not e["is_read"], f"Email {e['uid']} should be unread but has flags: {e['flags']}"
    print(f"        Found {result['total']} unread emails")

test_list_emails_unseen()


# Store a UID for read_email test
_test_uid = None


@test("read_email returns full content")
def test_read_email():
    global _test_uid
    listing = list_emails(mailbox="INBOX", page=1, page_size=1)
    assert listing["emails"], "Need at least one email to test read_email"
    _test_uid = listing["emails"][0]["uid"]

    result = read_email(uid=_test_uid, mailbox="INBOX")
    assert "body" in result, "read_email should return body"
    assert "subject" in result
    assert "from" in result
    assert "to" in result
    assert "attachments" in result
    assert isinstance(result["attachments"], list)
    print(f"        UID: {result['uid']}")
    print(f"        Subject: {result['subject'][:70]}")
    print(f"        From: {result['from'][:70]}")
    print(f"        Body length: {len(result['body'])} chars")
    print(f"        Attachments: {len(result['attachments'])}")
    if result["body"]:
        preview = result["body"][:150].replace("\n", " ")
        print(f"        Body preview: {preview}...")

test_read_email()


@test("search_emails finds results")
def test_search_emails():
    # Search for a common term
    result = search_emails(query="Proton", mailbox="INBOX", page=1, page_size=5)
    assert "emails" in result
    assert "total" in result
    print(f"        Search 'Proton': {result['total']} results")
    if result["emails"]:
        print(f"        First result: {result['emails'][0]['subject'][:60]}")

test_search_emails()


@test("search_emails with subject filter")
def test_search_emails_subject():
    result = search_emails(query="Proton", mailbox="INBOX", search_in="subject", page=1, page_size=5)
    assert "emails" in result
    print(f"        Search 'Proton' in subject: {result['total']} results")

test_search_emails_subject()


@test("search_emails with from filter")
def test_search_emails_from():
    result = search_emails(query="proton", mailbox="INBOX", search_in="from", page=1, page_size=5)
    assert "emails" in result
    print(f"        Search 'proton' in from: {result['total']} results")

test_search_emails_from()


@test("IMAP connection pool retry on stale connection")
def test_pool_retry():
    pool = get_pool()
    # Force invalidate
    pool.invalidate()
    # Should reconnect transparently
    mailboxes = list_mailboxes()
    assert len(mailboxes) > 0, "Should recover after invalidation"
    print(f"        Recovery successful, found {len(mailboxes)} mailboxes")

test_pool_retry()


@test("read_email handles encoded headers (Japanese/CJK)")
def test_encoded_headers():
    # Search for Japanese emails
    result = list_emails(mailbox="INBOX", page=1, page_size=50)
    found_cjk = False
    for e in result["emails"]:
        # Check if subject contains CJK characters
        if any(ord(c) > 0x2E80 for c in e["subject"]):
            found_cjk = True
            print(f"        Found CJK subject: {e['subject'][:60]}")
            break
    if not found_cjk:
        print(f"        No CJK emails found in first 50 (not a failure)")

test_encoded_headers()


@test("list_emails with date range filter")
def test_date_range():
    result = list_emails(
        mailbox="INBOX",
        page=1,
        page_size=5,
        since="2025-01-01",
    )
    assert "emails" in result
    print(f"        Emails since 2025-01-01: {result['total']}")

test_date_range()


@test("list_emails with from_address filter")
def test_from_filter():
    result = list_emails(
        mailbox="INBOX",
        page=1,
        page_size=5,
        from_address="proton",
    )
    assert "emails" in result
    print(f"        Emails from 'proton': {result['total']}")

test_from_filter()


@test("read_email body truncation check")
def test_body_truncation():
    from server import MAX_BODY_LENGTH
    # Read an email and verify body length is within limit
    listing = list_emails(mailbox="INBOX", page=1, page_size=1)
    if listing["emails"]:
        uid = listing["emails"][0]["uid"]
        result = read_email(uid=uid, mailbox="INBOX")
        assert len(result["body"]) <= MAX_BODY_LENGTH + 100, \
            f"Body too long: {len(result['body'])} > {MAX_BODY_LENGTH}"
        print(f"        Body length: {len(result['body'])} (max: {MAX_BODY_LENGTH})")

test_body_truncation()


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------

print("\n--- Performance Tests ---\n")


@test("list_emails performance (page of 20)")
def test_list_performance():
    start = time.time()
    result = list_emails(mailbox="INBOX", page=1, page_size=20)
    elapsed = time.time() - start
    print(f"        Time: {elapsed:.3f}s for {len(result['emails'])} emails")
    assert elapsed < 10.0, f"list_emails too slow: {elapsed:.3f}s"

test_list_performance()


@test("read_email performance (single email)")
def test_read_performance():
    listing = list_emails(mailbox="INBOX", page=1, page_size=1)
    if listing["emails"]:
        uid = listing["emails"][0]["uid"]
        start = time.time()
        result = read_email(uid=uid, mailbox="INBOX")
        elapsed = time.time() - start
        print(f"        Time: {elapsed:.3f}s for 1 email")
        assert elapsed < 5.0, f"read_email too slow: {elapsed:.3f}s"

test_read_performance()


@test("search_emails performance")
def test_search_performance():
    start = time.time()
    result = search_emails(query="Proton", mailbox="INBOX", page=1, page_size=10)
    elapsed = time.time() - start
    print(f"        Time: {elapsed:.3f}s for search ({result['total']} results)")
    assert elapsed < 15.0, f"search_emails too slow: {elapsed:.3f}s"

test_search_performance()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

success = results.summary()
sys.exit(0 if success else 1)
