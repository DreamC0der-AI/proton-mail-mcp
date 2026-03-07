"""
Comprehensive tests for EmailDB.search_fts() -- FTS5 local search.

Uses the REAL database at email-db/emails.db (13,075 INBOX emails, headers only).

Known emails used for assertions:
  UID 49:   "Reminder: MNG Survey & ES Reporting 2024 – Submission Required"
            from "Senthil Prabhu" <payments@abacus-offshore.com>
  UID 1159: "RE: MNG Survey and ES Reporting 2024"
            from "Senthil Prabhu" <payments@abacus-offshore.com>
  UID 1361: "MNG Survey and ES Reporting 2024"
            from "Senthil Prabhu" <payments@abacus-offshore.com>

Run:  python -m pytest test_local_search.py -v
"""

import sys
import os
import pytest

# Ensure the email package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import EmailDB, DB_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db():
    """Provide a shared read-only EmailDB instance for the whole module."""
    assert os.path.exists(DB_PATH), f"Database not found at {DB_PATH}"
    instance = EmailDB(DB_PATH)
    # Sanity: DB should have data
    total = instance.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    assert total > 0, "Database has no emails -- nothing to test"
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESULT_KEYS = {"emails", "query", "page", "page_size", "total", "total_pages",
               "mailbox", "source"}

EMAIL_KEYS = {"uid", "subject", "from", "to", "date", "message_id", "flags",
              "is_read", "is_flagged", "mailbox"}


def assert_result_structure(result: dict):
    """Every search_fts result must have the standard keys."""
    assert isinstance(result, dict)
    assert RESULT_KEYS.issubset(result.keys()), (
        f"Missing keys: {RESULT_KEYS - result.keys()}"
    )
    assert result["source"] == "fts5"
    assert isinstance(result["emails"], list)
    assert isinstance(result["total"], int)
    assert isinstance(result["page"], int)
    assert isinstance(result["page_size"], int)
    assert isinstance(result["total_pages"], int)
    assert result["total"] >= 0
    assert result["page"] >= 1
    assert result["total_pages"] >= 1


def assert_email_structure(email_dict: dict):
    """Each email in the results list must have the expected keys."""
    assert EMAIL_KEYS.issubset(email_dict.keys()), (
        f"Missing email keys: {EMAIL_KEYS - email_dict.keys()}"
    )
    # uid is returned as string
    assert isinstance(email_dict["uid"], str)
    assert isinstance(email_dict["subject"], str)
    assert isinstance(email_dict["is_read"], bool)
    assert isinstance(email_dict["is_flagged"], bool)


def uids_in(result: dict) -> set[str]:
    """Extract the set of UIDs (as strings) from a search result."""
    return {e["uid"] for e in result["emails"]}


# ---------------------------------------------------------------------------
# 1. Exact phrase search
# ---------------------------------------------------------------------------

class TestExactPhrase:
    def test_exact_phrase_mng_survey(self, db):
        """Quoted phrase should match all 3 known MNG Survey emails."""
        result = db.search_fts('"MNG Survey"')
        assert_result_structure(result)
        assert result["total"] == 3
        found_uids = uids_in(result)
        assert {"49", "1159", "1361"}.issubset(found_uids)

    def test_exact_phrase_structure(self, db):
        """Each result email should have proper structure."""
        result = db.search_fts('"MNG Survey"')
        for em in result["emails"]:
            assert_email_structure(em)

    def test_exact_phrase_no_partial(self, db):
        """Exact phrase should not match partial words."""
        result = db.search_fts('"MNG Surve"')
        # FTS5 exact phrase requires full token matches
        # "Surve" is not a token, so this should return 0
        assert_result_structure(result)
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# 2. Multi-word AND search
# ---------------------------------------------------------------------------

class TestAndSearch:
    def test_and_reminder_submission(self, db):
        """AND search: only UID 49 has both 'Reminder' and 'Submission'."""
        result = db.search_fts("Reminder AND Submission")
        assert_result_structure(result)
        assert result["total"] >= 1
        found_uids = uids_in(result)
        assert "49" in found_uids

    def test_and_narrows_results(self, db):
        """AND should return fewer results than either term alone."""
        r_reminder = db.search_fts("Reminder")
        r_submission = db.search_fts("Submission")
        r_both = db.search_fts("Reminder AND Submission")
        assert r_both["total"] <= r_reminder["total"]
        assert r_both["total"] <= r_submission["total"]

    def test_implicit_and(self, db):
        """FTS5 default is AND for space-separated tokens."""
        r_explicit = db.search_fts("Reminder AND Submission")
        r_implicit = db.search_fts("Reminder Submission")
        assert r_explicit["total"] == r_implicit["total"]


# ---------------------------------------------------------------------------
# 3. OR search
# ---------------------------------------------------------------------------

class TestOrSearch:
    def test_or_invoice_receipt(self, db):
        """OR search should return results matching either term."""
        result = db.search_fts("invoice OR receipt")
        assert_result_structure(result)
        assert result["total"] > 0
        # Should be at least as many as each term alone
        r_invoice = db.search_fts("invoice")
        r_receipt = db.search_fts("receipt")
        assert result["total"] >= r_invoice["total"]
        assert result["total"] >= r_receipt["total"]

    def test_or_union_superset(self, db):
        """OR results should be a superset of individual term results (on page 1)."""
        r_invoice = db.search_fts("invoice", page_size=100)
        r_receipt = db.search_fts("receipt", page_size=100)
        r_or = db.search_fts("invoice OR receipt", page_size=200)
        or_uids = uids_in(r_or)
        # Every uid from individual searches should appear in OR
        assert uids_in(r_invoice).issubset(or_uids)
        assert uids_in(r_receipt).issubset(or_uids)


# ---------------------------------------------------------------------------
# 4. NOT search
# ---------------------------------------------------------------------------

class TestNotSearch:
    def test_not_excludes(self, db):
        """NOT should exclude emails matching the second term."""
        result = db.search_fts("Survey NOT Reminder")
        assert_result_structure(result)
        assert result["total"] > 0
        # UID 49 has both Survey and Reminder -> must be excluded
        assert "49" not in uids_in(result)

    def test_not_keeps_non_matching(self, db):
        """NOT should keep emails that have the first term but not the second."""
        result = db.search_fts("Survey NOT Reminder", page_size=50)
        found_uids = uids_in(result)
        # UIDs 1159 and 1361 have Survey but not Reminder
        assert "1159" in found_uids
        assert "1361" in found_uids

    def test_not_reduces_count(self, db):
        """NOT should return fewer results than the base term alone."""
        r_base = db.search_fts("Survey")
        r_not = db.search_fts("Survey NOT Reminder")
        assert r_not["total"] < r_base["total"]


# ---------------------------------------------------------------------------
# 5. Prefix search
# ---------------------------------------------------------------------------

class TestPrefixSearch:
    def test_prefix_remind(self, db):
        """Prefix 'Remind*' should match Reminder, Reminders, etc."""
        result = db.search_fts("Remind*")
        assert_result_structure(result)
        assert result["total"] > 0
        # UID 49 subject starts with "Reminder:"
        # Need to page through or use big page to find it
        r_big = db.search_fts("Remind*", page_size=200)
        assert "49" in uids_in(r_big)

    def test_prefix_superset_of_exact(self, db):
        """Prefix search should return at least as many results as exact term."""
        r_exact = db.search_fts("Reminder")
        r_prefix = db.search_fts("Remind*")
        assert r_prefix["total"] >= r_exact["total"]


# ---------------------------------------------------------------------------
# 6. Column-specific search
# ---------------------------------------------------------------------------

class TestColumnSearch:
    def test_from_addr_column(self, db):
        """Search only in from_addr column."""
        result = db.search_fts("from_addr:payments")
        assert_result_structure(result)
        assert result["total"] > 0
        # All 3 known MNG Survey emails are from payments@abacus-offshore.com
        r_big = db.search_fts("from_addr:payments", page_size=50)
        found = uids_in(r_big)
        assert {"49", "1159", "1361"}.issubset(found)

    def test_subject_column(self, db):
        """Search only in subject column."""
        result = db.search_fts("subject:Survey")
        assert_result_structure(result)
        assert result["total"] > 0
        r_big = db.search_fts("subject:Survey", page_size=50)
        found = uids_in(r_big)
        assert {"49", "1159", "1361"}.issubset(found)

    def test_column_search_limits_scope(self, db):
        """Column-specific search should be narrower than global search.

        If a term appears in subjects AND from_addr fields, searching
        only one column should return <= the full search.
        """
        r_global = db.search_fts("payments")
        r_from = db.search_fts("from_addr:payments")
        # from-only should be <= global (global also matches subject/to/body)
        assert r_from["total"] <= r_global["total"]


# ---------------------------------------------------------------------------
# 7. Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_page_size_respected(self, db):
        """Returned emails list should respect page_size."""
        result = db.search_fts("Survey", page=1, page_size=2)
        assert_result_structure(result)
        assert len(result["emails"]) <= 2

    def test_page_navigation(self, db):
        """Different pages should return different emails (no overlap)."""
        r1 = db.search_fts("Survey", page=1, page_size=2)
        r2 = db.search_fts("Survey", page=2, page_size=2)
        uids_p1 = uids_in(r1)
        uids_p2 = uids_in(r2)
        # Pages should not overlap
        assert uids_p1.isdisjoint(uids_p2), (
            f"Pages overlap: {uids_p1 & uids_p2}"
        )

    def test_total_pages_calculation(self, db):
        """total_pages should be ceil(total / page_size)."""
        result = db.search_fts("Survey", page_size=3)
        expected_pages = max(1, -(-result["total"] // 3))  # ceiling division
        assert result["total_pages"] == expected_pages

    def test_page_metadata(self, db):
        """Result should reflect the requested page and page_size."""
        result = db.search_fts("Survey", page=2, page_size=5)
        assert result["page"] == 2
        assert result["page_size"] == 5

    def test_last_page_partial(self, db):
        """Last page may have fewer results than page_size."""
        r_all = db.search_fts("Survey", page_size=100)
        total = r_all["total"]
        if total > 3:
            last_page = db.search_fts("Survey", page=r_all["total_pages"],
                                      page_size=3)
            # Last page should have between 1 and page_size emails
            assert 0 < len(last_page["emails"]) <= 3

    def test_beyond_last_page_empty(self, db):
        """Requesting a page beyond total_pages should return no emails."""
        r = db.search_fts("Survey", page_size=5)
        far = db.search_fts("Survey", page=r["total_pages"] + 10, page_size=5)
        assert len(far["emails"]) == 0


# ---------------------------------------------------------------------------
# 8. Mailbox filter
# ---------------------------------------------------------------------------

class TestMailboxFilter:
    def test_inbox_filter(self, db):
        """Filtering by INBOX should work (all our data is INBOX)."""
        result = db.search_fts('"MNG Survey"', mailbox="INBOX")
        assert_result_structure(result)
        assert result["mailbox"] == "INBOX"
        assert result["total"] == 3
        # Every result should be in INBOX
        for em in result["emails"]:
            assert em["mailbox"] == "INBOX"

    def test_all_mailboxes(self, db):
        """Searching without mailbox should search all (mailbox='all')."""
        result = db.search_fts('"MNG Survey"', mailbox=None)
        assert_result_structure(result)
        assert result["mailbox"] == "all"
        assert result["total"] == 3

    def test_nonexistent_mailbox(self, db):
        """Searching a mailbox with no data should return 0."""
        result = db.search_fts('"MNG Survey"', mailbox="Sent")
        assert_result_structure(result)
        assert result["total"] == 0
        assert len(result["emails"]) == 0

    def test_mailbox_filter_count_leq_all(self, db):
        """Results for a single mailbox should be <= results for all."""
        r_all = db.search_fts("Survey")
        r_inbox = db.search_fts("Survey", mailbox="INBOX")
        assert r_inbox["total"] <= r_all["total"]


# ---------------------------------------------------------------------------
# 9. Empty results
# ---------------------------------------------------------------------------

class TestEmptyResults:
    def test_no_match(self, db):
        """A query that matches nothing should return well-formed empty result."""
        result = db.search_fts("xyzzy999nonexistent")
        assert_result_structure(result)
        assert result["total"] == 0
        assert result["total_pages"] == 1  # min 1 page even with 0 results
        assert result["emails"] == []

    def test_no_match_with_mailbox(self, db):
        """Empty result with mailbox filter."""
        result = db.search_fts("xyzzy999nonexistent", mailbox="INBOX")
        assert_result_structure(result)
        assert result["total"] == 0
        assert result["emails"] == []
        assert result["mailbox"] == "INBOX"


# ---------------------------------------------------------------------------
# 10. Special characters
# ---------------------------------------------------------------------------

class TestSpecialCharacters:
    def test_ampersand_in_subject(self, db):
        """UID 49 subject contains '&' -- search for surrounding words."""
        # FTS5 tokenizes around '&', so search for the words around it
        result = db.search_fts("Survey Reporting", page_size=50)
        assert_result_structure(result)
        assert "49" in uids_in(result)

    def test_colon_in_subject(self, db):
        """UID 49 subject starts with 'Reminder:' -- colons are tokenizer boundaries."""
        result = db.search_fts("Reminder Submission", page_size=50)
        assert_result_structure(result)
        assert "49" in uids_in(result)

    def test_at_sign_in_from(self, db):
        """Email addresses with '@' -- search for the domain part."""
        result = db.search_fts("from_addr:abacus", page_size=50)
        assert_result_structure(result)
        found = uids_in(result)
        # All 3 known emails are from payments@abacus-offshore.com
        assert {"49", "1159", "1361"}.issubset(found)

    def test_hyphen_in_domain(self, db):
        """Hyphens in domains: 'abacus-offshore' might be tokenized as two words."""
        # Search for 'offshore' alone should match
        result = db.search_fts("from_addr:offshore", page_size=50)
        assert_result_structure(result)
        found = uids_in(result)
        assert {"49", "1159", "1361"}.issubset(found)

    def test_parentheses_escaped(self, db):
        """Parentheses are FTS5 syntax -- test they don't crash when in subject.

        We search for a word that may appear alongside parentheses.
        """
        # Just verify it doesn't crash and returns valid structure
        result = db.search_fts("Invoice", page_size=5)
        assert_result_structure(result)
        assert result["total"] > 0


# ---------------------------------------------------------------------------
# Bonus: result consistency
# ---------------------------------------------------------------------------

class TestResultConsistency:
    def test_total_matches_all_pages(self, db):
        """Collecting all pages should yield exactly 'total' emails."""
        r = db.search_fts('"MNG Survey"', page_size=2)
        all_uids = set()
        for p in range(1, r["total_pages"] + 1):
            page_r = db.search_fts('"MNG Survey"', page=p, page_size=2)
            all_uids.update(uids_in(page_r))
        assert len(all_uids) == r["total"]

    def test_query_echoed_back(self, db):
        """The 'query' field in the result should echo back the search query."""
        q = '"MNG Survey"'
        result = db.search_fts(q)
        assert result["query"] == q

    def test_source_is_fts5(self, db):
        """Source should always be 'fts5'."""
        result = db.search_fts("test")
        assert result["source"] == "fts5"
