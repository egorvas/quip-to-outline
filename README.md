# Quip to Outline Migration Tool

Direct migration from Quip to self-hosted Outline wiki via API. No intermediate files — fetches documents from Quip API and imports directly into Outline.

## What it does

- Fetches documents directly from Quip API (no HTML export step needed)
- Preserves folder hierarchy (Quip spaces -> Outline collections, folders -> parent docs)
- Downloads images/files with parallel downloads, uploads as Outline attachments
- Creates comments from Quip Messages API with quoted context text
- Automatically creates Outline users from Quip authors
- Sets correct timestamps and authors on documents and comments
- Syncs folder permissions (Quip folder members -> Outline collection access)
- Adaptive Quip API rate limiting based on response headers
- Resume support — safe to re-run after interruption

## Prerequisites

- Python 3.9+
- Self-hosted Outline instance with PostgreSQL access
- Quip API token — get it at https://quip.com/dev/token
- Outline API token — Settings -> API in your Outline instance

## Installation

```bash
pip install quip-to-outline
```

Or with pipx (isolated environment):

```bash
pipx install quip-to-outline
```

Or run directly without installing:

```bash
pipx run quip-to-outline --init
pipx run quip-to-outline
```

**Recommended:** Disable Outline rate limiting before import. Add to your Outline environment:

```
RATE_LIMITER_ENABLED=false
```

Restart Outline, then re-enable after migration.

## Usage

### Step 1: Generate config

```bash
quip-to-outline --init
```

Edit `config.json`:

```json
{
  "outline_url": "http://your-outline:3000",
  "outline_api_token": "ol_api_...",
  "quip_api_token": "YOUR_QUIP_TOKEN",
  "db_host": "localhost",
  "db_port": 5432,
  "db_user": "outline",
  "db_password": "your_db_password",
  "db_name": "outline",
  "quip_concurrency": 5,
  "blob_concurrency": 8
}
```

| Field | Description |
|-------|-------------|
| `outline_url` | URL of your Outline instance |
| `outline_api_token` | Outline API token (Settings -> API) |
| `quip_api_token` | Quip API token (https://quip.com/dev/token) |
| `db_host` | PostgreSQL host for Outline database |
| `db_port` | PostgreSQL port |
| `db_user` | PostgreSQL user |
| `db_password` | PostgreSQL password |
| `db_name` | PostgreSQL database name |
| `quip_concurrency` | Max parallel Quip API requests (default: 5) |
| `blob_concurrency` | Max parallel image/file downloads (default: 8) |

### Step 2: Run migration

```bash
quip-to-outline
```

The script runs through these phases:

1. **Walk Quip folders** — recursively discovers all folders and threads via API
2. **Fetch thread data** — batch-fetches metadata + HTML for all threads in one pass
3. **Create users** — matches Quip authors to existing Outline users; creates new users for unmatched authors
4. **Import documents** — downloads images in parallel, uploads as Outline attachments, imports HTML via Outline API, creates comments with quoted context
5. **Update DB** — sets original Quip timestamps and authors on documents and comments

### Migration options

```bash
quip-to-outline [options]
```

| Option | Description |
|--------|-------------|
| `--noComments` | Skip comment migration. Saves 1 Quip API request per document. |
| `--noAttachments` | Skip image/file downloads. Text-only import, much faster. |
| `--noUsers` | Skip user creation. Implies `--noPermissions`. All content attributed to admin. |
| `--noPermissions` | Skip permission sync. Collections accessible to everyone. |
| `--folders a,b,c` | Only migrate specified folders (comma-separated names). |
| `--resetCache` | Clear cached Quip data, re-fetch from API. Import progress is preserved. |

When `--noUsers` is set but comments are enabled, comments include the original author name and timestamp in the text:

```
[John Doe, 25 Sep 2025 12:43]
Comment text here
```

#### Examples

```bash
# Full migration (all features)
quip-to-outline

# Text only, no images (fastest)
quip-to-outline --noAttachments

# Only specific folders
quip-to-outline --folders devOps,kosAccess

# No user management, just content
quip-to-outline --noUsers --noComments

# Import with comments but without images and permissions
quip-to-outline --noAttachments --noPermissions

# Re-fetch Quip data after adding new documents
quip-to-outline --resetCache
```

### Caching and resume

The script caches Quip API data in `state.json` to minimize API calls on re-run:

| Data | Cached | On restart |
|------|--------|------------|
| Folder tree | Yes | 0 Quip API calls |
| Thread metadata | Yes | Only new threads fetched |
| User names | Yes | Only new users resolved |
| Thread HTML | No (too large) | Fetched for non-imported threads |
| Import progress | Yes | Skips already imported |

On restart, the script picks up exactly where it left off — no wasted API calls.

- `--resetCache` clears the Quip data cache but preserves import progress
- `--folders` applies a filter on the cached tree — no extra API calls
- Delete `state.json` entirely to start from scratch

## How it works

### Quip API rate limiting

Quip allows ~50 requests/minute per user and ~600/minute per company. The script reads rate limit headers (`X-Ratelimit-Remaining`, `X-Ratelimit-Reset`) from every response and automatically:
- Sends requests as fast as the limit allows
- Pauses when quota is exhausted, waiting exactly until the reset time
- Backs off adaptively on 429/503 errors

### Images and attachments

Images referenced in Quip documents are downloaded in parallel (configurable via `blob_concurrency`) and uploaded to Outline as native attachments. The HTML is updated with Outline attachment URLs before import. This avoids bloating documents with base64-encoded images.

### Comments

Comments are fetched via Quip Messages API (`/messages/{thread_id}`), which provides structured data: author ID, exact timestamp (microsecond precision), and text. If the comment was attached to specific text in Quip (annotation), the quoted text is included in the comment as an italic citation.

### Permissions

Quip folder `member_ids` are mapped to Outline collection permissions. All folder members get `read_write` access to the corresponding collection.

## Generated files

| File | Description |
|------|-------------|
| `config.json` | Credentials — **do not share or commit** |
| `state.json` | Migration progress — tracks imported threads for resume |
| `author_mapping.json` | Quip author name -> Outline user ID mapping |

## Troubleshooting

**Quip 503 errors** — rate limit exceeded. The script handles this automatically, but if you see many retries, reduce `quip_concurrency` in config.

**Outline 400 on large documents** — some documents with many large images may exceed Outline's body size limit. The script uploads images as separate attachments to avoid this. If it still fails, try `--noAttachments`.

**Resume after crash** — just re-run. The script reads `state.json` and skips already-imported documents.

**Wrong permissions** — delete the collection in Outline and re-run. Or use `--noPermissions` and set permissions manually.
