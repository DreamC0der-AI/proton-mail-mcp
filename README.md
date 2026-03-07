# Proton Mail MCP Server

An MCP server for [Claude Code](https://claude.com/claude-code) that provides email access through [Proton Mail Bridge](https://proton.me/mail/bridge). Emails are synced to a local SQLite database with FTS5 full-text search for fast offline access.

## Features

- **13 MCP tools**: list, read, search, send, reply, move, mark, delete emails + attachment downloads
- **Local SQLite database**: Sync email headers once, work offline. Bodies fetched lazily on demand
- **FTS5 full-text search**: Search across subject, sender, recipient, and body — instant results across 13k+ emails
- **Incremental sync**: First sync ~2-3 min, subsequent syncs in seconds
- **DB-first architecture**: `list_emails`, `read_email`, `search_emails` serve from local DB when synced, IMAP fallback otherwise

## Setup

### Prerequisites

- Python 3.10+
- [Proton Mail Bridge](https://proton.me/mail/bridge) running locally
- `python-dotenv`, `mcp` packages

### 1. Clone and configure

```bash
git clone https://github.com/DreamC0der-AI/proton-mail-mcp.git
cd proton-mail-mcp
```

Create a `.env` file with your Proton Bridge credentials:

```env
EMAIL_USER=your@pm.me
EMAIL_PASS=your-bridge-password
IMAP_HOST=127.0.0.1
IMAP_PORT=1143
SMTP_HOST=127.0.0.1
SMTP_PORT=1025
```

### 2. Add to Claude Code

Copy `example.mcp.json` to your project's `.mcp.json` and update the path:

```json
{
  "mcpServers": {
    "email": {
      "type": "stdio",
      "command": "python3",
      "args": ["/absolute/path/to/proton-mail-mcp/server.py"]
    }
  }
}
```

### 3. Sync emails

After restarting Claude Code, run `sync_emails` to pull headers into the local database. Then use `local_search` for instant full-text search.

## Tools

| Tool | Description |
|------|-------------|
| `sync_emails` | Sync headers from IMAP to local DB (incremental) |
| `sync_status` | Show sync state per mailbox |
| `local_search` | FTS5 search: `"exact phrase"`, `word*`, `a AND b`, `a NOT b` |
| `list_emails` | List emails with filters (DB-first when synced) |
| `read_email` | Read full email by UID (lazy body fetch + cache) |
| `search_emails` | Search emails (FTS5 when synced, IMAP fallback) |
| `send_email` | Send email via SMTP |
| `reply_email` | Reply with proper threading headers |
| `mark_email` | Mark as read/unread/flagged/unflagged |
| `move_email` | Move between mailboxes |
| `delete_email` | Move to Trash |
| `download_attachment` | Download and track attachments |
| `list_downloads` | List downloaded attachments |
| `list_mailboxes` | List available mailboxes |

## Architecture

```
server.py          MCP server (FastMCP, IMAP pool, SMTP, tool definitions)
db.py              EmailDB (SQLite + FTS5) + EmailSyncer (IMAP-to-DB sync)
.env               Credentials (gitignored)
data/              Local database + attachments (gitignored)
  emails.db        SQLite WAL database
  attachments/     Downloaded attachment files
tests/             Test suite
```

## License

MIT
