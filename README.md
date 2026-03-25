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
- Self-hosted Outline instance
- Quip API token — <https://quip.com/dev/token>
- Outline API token — Settings -> API in your Outline instance
- (Optional) PostgreSQL access to Outline database — for timestamp/author updates

## Installation

```bash
pipx install git+https://github.com/egorvas/quip-to-outline.git
```

Or with pip:

```bash
pip install git+https://github.com/egorvas/quip-to-outline.git
```

**Recommended:** Disable Outline rate limiting before import. Add to your Outline environment:

```text
RATE_LIMITER_ENABLED=false
```

Restart Outline, then re-enable after migration.

## Quick start

```bash
quip-to-outline --init          # 1. Generate config.json
# edit config.json               # 2. Fill in credentials
quip-to-outline --list          # 3. Preview Quip folders
quip-to-outline --dryRun        # 4. See what will be imported
quip-to-outline                 # 5. Run migration
quip-to-outline --verify        # 6. Check results
```

## Configuration

### Generate config

```bash
quip-to-outline --init
quip-to-outline --config ~/migration/config.json --init   # custom path
```

### Minimal config (API only)

```json
{
  "outline_url": "http://your-outline:3000",
  "outline_api_token": "ol_api_...",
  "quip_api_token": "YOUR_QUIP_TOKEN",
  "quip_concurrency": 5,
  "blob_concurrency": 8
}
```

### Full config (with database)

```json
{
  "outline_url": "http://your-outline:3000",
  "outline_api_token": "ol_api_...",
  "quip_api_token": "YOUR_QUIP_TOKEN",
  "quip_concurrency": 5,
  "blob_concurrency": 8,
  "db_host": "localhost",
  "db_port": 5432,
  "db_user": "outline",
  "db_password": "your_db_password",
  "db_name": "outline"
}
```

### Config fields

| Field               | Required | Description                                                  |
| ------------------- | -------- | ------------------------------------------------------------ |
| `outline_url`       | Yes      | URL of your Outline instance                                 |
| `outline_api_token` | Yes      | Outline API token (Settings -> API)                          |
| `quip_api_token`    | Yes      | Quip API token                                               |
| `quip_concurrency`  | No       | Max parallel Quip API requests (default: 5)                  |
| `blob_concurrency`  | No       | Max parallel image/file downloads (default: 8)               |
| `db_host`           | No       | PostgreSQL host — **if set, enables DB features**            |
| `db_port`           | No       | PostgreSQL port (default: 5432)                              |
| `db_user`           | No       | PostgreSQL user (default: outline)                           |
| `db_password`       | No       | PostgreSQL password (can be empty if no auth)                |
| `db_name`           | No       | PostgreSQL database name (default: outline)                  |

Without database configuration, the script imports all documents, comments, and creates users — but original Quip timestamps and author attribution won't be set.

## Commands

### `--init` — Generate config template

```bash
quip-to-outline --init
```

Creates `config.json` in the current directory (or path from `--config`).

### `--list` — Preview Quip folders

```bash
quip-to-outline --list
```

Shows the Quip folder tree without importing anything. Useful for choosing folders for `--folders` / `--noFolders`.

### `--dryRun` — Simulate migration

```bash
quip-to-outline --dryRun
quip-to-outline --dryRun --folders devOps,kosAccess
```

Shows a breakdown of what would be imported: documents per folder, how many are new vs already imported, which features are enabled.

### `--status` — Show migration progress

```bash
quip-to-outline --status
```

Shows current state: imported documents, comments, collections, cache status, unmapped authors, and any documents not yet imported.

### `--verify` — Validate migration

```bash
quip-to-outline --verify
```

Compares Quip threads vs Outline documents and reports:
- Documents in Quip but missing in Outline
- Documents in Outline state but deleted from Outline
- Total match count

### `--retry` — Re-import failed documents

```bash
quip-to-outline --retry
```

Clears cached data for documents that failed in a previous run and re-imports only those. Requires a previous run with cached data.

### `--cleanup` — Remove everything from Outline

```bash
quip-to-outline --cleanup
```

Deletes all documents, collections, and folder docs created by the script (tracked in `state.json`). Asks for confirmation. Preserves cache so you can re-import without re-fetching from Quip.

### (default) — Run migration

```bash
quip-to-outline
```

Runs through all phases:

1. **Walk Quip folders** — recursively discovers all folders and threads via API
2. **Fetch thread data** — batch-fetches metadata + HTML for all threads in one pass
3. **Create users** — matches Quip authors to existing Outline users; creates new users for unmatched
4. **Import documents** — downloads images in parallel, uploads as Outline attachments, imports HTML, creates comments with quoted context
5. **Update DB** — sets original Quip timestamps and authors (if database configured)

## Options

| Option              | Description                                                                             |
| ------------------- | --------------------------------------------------------------------------------------- |
| `--config PATH`     | Path to config.json (default: `./config.json`). All files stored next to config.        |
| `--noComments`      | Skip comment migration. Saves 1 Quip API request per document.                         |
| `--noAttachments`   | Skip image/file downloads. Text-only import, much faster.                               |
| `--noUsers`         | Skip user creation. Implies `--noPermissions`. All content attributed to admin.         |
| `--noPermissions`   | Skip permission sync. Collections accessible to everyone.                               |
| `--folders a,b,c`   | Only migrate specified folders (comma-separated names).                                 |
| `--noFolders a,b,c` | Exclude specified folders from migration.                                               |
| `--resetCache`      | Clear cached Quip data, re-fetch from API. Import progress preserved.                   |

`--folders` and `--noFolders` can be used together, but the same folder in both is an error.

When `--noUsers` is set but comments are enabled, comments include the original author name and timestamp:

```text
[John Doe, 25 Sep 2025 12:43]
Comment text here
```

## Examples

```bash
# Full migration
quip-to-outline

# Custom config location
quip-to-outline --config ~/migration/config.json

# Text only, no images (fastest)
quip-to-outline --noAttachments

# Only specific folders
quip-to-outline --folders devOps,kosAccess

# Everything except archive
quip-to-outline --noFolders Archive,Feedbacks

# Minimal: docs only, no users/comments
quip-to-outline --noUsers --noComments

# Re-fetch after new docs added in Quip
quip-to-outline --resetCache

# Check what will be imported before running
quip-to-outline --dryRun --folders kosAccess

# Retry only failed documents
quip-to-outline --retry

# Start over: remove from Outline, re-import
quip-to-outline --cleanup
quip-to-outline
```

## Caching and resume

The script caches Quip API data in `state.json` to minimize API calls on re-run:

| Data            | Cached | On restart                       |
| --------------- | ------ | -------------------------------- |
| Folder tree     | Yes    | 0 Quip API calls                 |
| Thread metadata | Yes    | Only new threads fetched         |
| User names      | Yes    | Only new users resolved          |
| Thread HTML     | No     | Fetched for non-imported threads |
| Import progress | Yes    | Skips already imported           |

On restart, the script picks up exactly where it left off — no wasted API calls.

- `--resetCache` clears the Quip data cache but preserves import progress
- `--folders` / `--noFolders` filter the cached tree — no extra API calls
- `--status` shows what's cached and what's pending
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

Comments are fetched via Quip Messages API, which provides structured data: author ID, exact timestamp (microsecond precision), and text. If the comment was attached to specific text in Quip (annotation), the quoted text is included in the comment as an italic citation.

### Permissions

Quip folder `member_ids` are mapped to Outline collection permissions. All folder members get `read_write` access to the corresponding collection.

## Generated files

| File                  | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `config.json`         | Credentials — **do not share or commit**                |
| `state.json`          | Migration progress and cache — tracks imported threads  |
| `author_mapping.json` | Quip author name -> Outline user ID mapping             |

## Troubleshooting

**Quip 503 errors** — rate limit exceeded. The script handles this automatically, but if you see many retries, reduce `quip_concurrency` in config.

**Outline 400 on large documents** — some documents with many large images may exceed Outline's body size limit. The script uploads images as separate attachments to avoid this. If it still fails, try `--noAttachments`.

**Resume after crash** — just re-run. The script reads `state.json` and skips already-imported documents. Or use `--retry` to re-import only failed ones.

**Wrong permissions** — use `--cleanup` and re-run, or `--noPermissions` to set them manually.

**Verify results** — run `--verify` after migration to check for missing documents.
