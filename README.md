# Quip to Outline Migration Tool

Direct migration from Quip to self-hosted Outline wiki via API. No intermediate files — fetches documents from Quip API and imports directly into Outline.

## What it does

- Fetches documents directly from Quip API with local caching (HTML and blobs)
- Fixes Quip HTML for Outline compatibility (code blocks, monospace, headings)
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
quip-to-outline                 # 4. Run migration
quip-to-outline --status        # 5. Check results
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
  "blob_concurrency": 8,
  "outline_concurrency": 4
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
  "outline_concurrency": 4,
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
| `outline_concurrency` | No     | Max parallel document imports to Outline (default: 4)        |
| `db_host`           | No       | PostgreSQL host — **if set, enables DB features**            |
| `db_port`           | No       | PostgreSQL port (default: 5432)                              |
| `db_user`           | No       | PostgreSQL user (default: outline)                           |
| `db_password`       | No       | PostgreSQL password (can be empty if no auth)                |
| `db_name`           | No       | PostgreSQL database name (default: outline)                  |

Without database configuration, the script imports all documents, comments, and creates users — but original Quip timestamps and author attribution won't be set.

For `--prefetch` mode only `quip_api_token` is required; `outline_url` and `outline_api_token` can be left empty.

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

### `--prefetch` — Download everything from Quip without importing

```bash
quip-to-outline --prefetch
quip-to-outline --prefetch --folders devOps,kosAccess
```

Populates local caches (`html_cache/`, `blob_cache/`, `msg_cache/`) with all Quip data but does not touch Outline. Outline credentials are not required — only `quip_api_token` must be set in config. Supports `--folders` / `--noFolders` / `--noComments` / `--noAttachments` / `--private` / `--desktop`. Safe to re-run — only missing data is fetched.

### `--status` — Show migration progress

```bash
quip-to-outline --status
```

Shows current state: imported documents, comments, collections, cache status, unmapped authors, and any documents not yet imported. Also verifies Outline side.

### `--updateDb` — Re-run DB update phase

```bash
quip-to-outline --updateDb
quip-to-outline --updateDb --fixUpdated=90
```

Re-runs only the database update phase (timestamps and authors) for all imported docs, without re-importing. Requires database configuration. Combine with `--fixUpdated=N` to fix stale `updated_at` without touching Outline API.

### `--cleanup` — Remove everything from Outline

```bash
quip-to-outline --cleanup
```

Deletes all documents, collections, and folder docs created by the script (tracked in `state.json`). Asks for confirmation. Also clears all local caches.

### `--resetCache` — Clear local caches

```bash
quip-to-outline --resetCache
```

Clears `html_cache/`, `blob_cache/`, `msg_cache/` and the in-state cache (folder tree, thread metadata, user names). Preserves import progress.

### `--resetState` — Reset import progress

```bash
quip-to-outline --resetState
```

Resets import progress (imported threads, collections, folder docs) but keeps all caches. Useful for re-importing the same Quip content into a fresh Outline instance without re-fetching from Quip.

### (default) — Run migration

```bash
quip-to-outline
```

Runs through all phases:

1. **Walk Quip folders** — recursively discovers all folders and threads via API
2. **Fetch thread data** — batch-fetches metadata + HTML (cached locally in `html_cache/`)
3. **Create users** — matches Quip authors to existing Outline users; creates new users for unmatched
4. **Import documents** — downloads images in parallel (cached in `blob_cache/`), fixes HTML for Outline, uploads as attachments, imports via API, creates comments
5. **Update DB** — sets original Quip timestamps and authors (if database configured)

## Options

| Option              | Description                                                                                                       |
| ------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `--config PATH`     | Path to config.json (default: `./config.json`). All files stored next to config.                                  |
| `--noComments`      | Skip comment migration. Saves 1 Quip API request per document.                                                    |
| `--noAttachments`   | Skip image/file downloads. Text-only import, much faster.                                                         |
| `--noUsers`         | Skip user creation. Implies `--noPermissions`. All content attributed to admin.                                   |
| `--noPermissions`   | Skip permission sync. Collections accessible to everyone.                                                         |
| `--folders a,b,c`   | Only migrate specified folders (comma-separated names).                                                           |
| `--noFolders a,b,c` | Exclude specified folders from migration.                                                                         |
| `--private`         | Include personal Private folder (excluded by default).                                                            |
| `--desktop`         | Include personal Desktop folder (excluded by default).                                                            |
| `--fixUpdated=N`    | Fix stale `updated_at`: if `updated-created > N days`, reset `updated_at` to `created_at`. Used with `--updateDb`.|

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

# Include personal folders
quip-to-outline --private --desktop

# Re-fetch after new docs added in Quip
quip-to-outline --resetCache

# Cache everything from Quip without importing (no Outline creds needed)
quip-to-outline --prefetch

# Re-import into a fresh Outline without re-fetching from Quip
quip-to-outline --resetState
quip-to-outline

# Start over: remove from Outline, clear caches, re-import
quip-to-outline --cleanup
quip-to-outline

# Fix stale updated_at dates without re-importing
quip-to-outline --updateDb --fixUpdated=90
```

## Caching and resume

The script caches Quip API data in `state.json` to minimize API calls on re-run:

| Data            | Cached | On restart                       |
| --------------- | ------ | -------------------------------- |
| Folder tree     | Yes    | 0 Quip API calls                 |
| Thread metadata | Yes    | Only new threads fetched         |
| User names      | Yes    | Only new users resolved          |
| Thread HTML     | Yes    | File cache in `html_cache/`      |
| Blob/images     | Yes    | File cache in `blob_cache/`      |
| Comments        | Yes    | File cache in `msg_cache/`       |
| Import progress | Yes    | Skips already imported           |

On restart, the script picks up exactly where it left off — no wasted API calls.

- `--resetCache` clears the Quip data cache but preserves import progress
- `--resetState` clears import progress but keeps the cache — useful for re-importing into a fresh Outline
- Delete `html_cache/`, `blob_cache/`, `msg_cache/` to force re-download from Quip
- `--folders` / `--noFolders` filter the cached tree — no extra API calls
- `--status` shows what's cached and what's pending
- `--prefetch` downloads everything without touching Outline

## How it works

### Quip API rate limiting

Quip allows ~50 requests/minute per user and ~600/minute per company. The script reads rate limit headers (`X-Ratelimit-Remaining`, `X-Ratelimit-Reset`) from every response and automatically:

- Sends requests as fast as the limit allows
- Pauses when quota is exhausted, waiting exactly until the reset time
- Backs off adaptively on 429/503 errors

### Images and attachments

Images referenced in Quip documents are downloaded in parallel (configurable via `blob_concurrency`) and cached locally in `blob_cache/`. On subsequent runs, cached blobs are reused without Quip API calls. Blobs are uploaded to Outline as native attachments, and the HTML is updated with Outline attachment URLs before import.

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
| `html_cache/`         | Cached Quip thread HTML + metadata (one JSON per thread)|
| `blob_cache/`         | Cached Quip images/files (binary + `.meta` content-type)|
| `msg_cache/`          | Cached Quip comments/messages (one JSON per thread)     |

## Troubleshooting

**Quip 503 errors** — rate limit exceeded. The script handles this automatically, but if you see many retries, reduce `quip_concurrency` in config.

**Outline 400 on large documents** — some documents with many large images may exceed Outline's body size limit. The script uploads images as separate attachments to avoid this. If it still fails, try `--noAttachments`.

**Resume after crash** — just re-run. The script reads `state.json` and skips already-imported documents.

**Wrong permissions** — use `--cleanup` and re-run, or `--noPermissions` to set them manually.

**Verify results** — run `--status` after migration to check imported counts and any missing documents.
