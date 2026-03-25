#!/usr/bin/env python3
"""
Direct Quip-to-Outline migration.

Fetches documents directly from Quip API and imports into Outline.
No intermediate HTML files — everything in one pass.

Usage:
  1. python3 migrate.py --init         Generate config.json
  2. Edit config.json
  3. python3 migrate.py                Run migration

Features:
  - Parallel blob downloads (images/files) via ThreadPoolExecutor
  - Adaptive Quip API rate limiting
  - Comments via Quip Messages API (structured, with exact timestamps)
  - Auto-creates Outline users from Quip authors
  - Resume support via state.json
  - Updates timestamps and authors in Outline DB

Requirements:
  pip install psycopg2-binary
"""

import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

# --- Transliteration ---

_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def transliterate(text):
    result = []
    for ch in text:
        lower = ch.lower()
        if lower in _TRANSLIT:
            tr = _TRANSLIT[lower]
            result.append(tr.upper() if ch.isupper() and tr else tr)
        else:
            result.append(ch)
    return ''.join(result)


# --- Config & State ---

WORK_DIR = os.getcwd()
CONFIG_FILE = os.path.join(WORK_DIR, "config.json")
STATE_FILE = os.path.join(WORK_DIR, "state.json")
MAPPING_FILE = os.path.join(WORK_DIR, "author_mapping.json")

CONFIG_TEMPLATE = {
    "outline_url": "http://localhost:3000",
    "outline_api_token": "",
    "quip_api_token": "",
    "quip_concurrency": 5,
    "blob_concurrency": 8,
    "_comment": "Database is optional. Remove this line and fill db fields to enable timestamp/author updates.",
    "db_host": "",
    "db_port": 5432,
    "db_user": "outline",
    "db_password": "",
    "db_name": "outline",
}

# Globals set by setup_globals()
OUTLINE_URL = ""
OUTLINE_TOKEN = ""
QUIP_TOKEN = ""
DB_CONFIG = {}
DB_ENABLED = False
QUIP_CONCURRENCY = 5
BLOB_CONCURRENCY = 8

# Migration mode flags (set from CLI args)
OPT_NO_COMMENTS = False
OPT_NO_PERMISSIONS = False
OPT_NO_ATTACHMENTS = False
OPT_NO_USERS = False
OPT_FOLDERS = None  # None = all, or set of folder names to include


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: config.json not found. Run: python3 {sys.argv[0]} --init")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    required = ["outline_url", "outline_api_token", "quip_api_token"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"Error: missing config fields: {', '.join(missing)}")
        sys.exit(1)
    return cfg


def setup_globals(cfg):
    global OUTLINE_URL, OUTLINE_TOKEN, QUIP_TOKEN, DB_CONFIG, DB_ENABLED, QUIP_CONCURRENCY, BLOB_CONCURRENCY
    OUTLINE_URL = cfg["outline_url"].rstrip("/")
    OUTLINE_TOKEN = cfg["outline_api_token"]
    QUIP_TOKEN = cfg["quip_api_token"]
    QUIP_CONCURRENCY = cfg.get("quip_concurrency", 5)
    BLOB_CONCURRENCY = cfg.get("blob_concurrency", 8)

    # DB is optional — enabled only if db_host is set and psycopg2 available
    if cfg.get("db_host"):
        if not HAS_PSYCOPG2:
            print("Warning: db configured but psycopg2 not installed. DB features disabled.")
            print("  Install: pip install psycopg2-binary")
            DB_ENABLED = False
        else:
            DB_CONFIG = {
                "host": cfg.get("db_host", "localhost"),
                "port": int(cfg.get("db_port", 5432)),
                "user": cfg.get("db_user", "outline"),
                "password": cfg["db_password"],
                "dbname": cfg.get("db_name", "outline"),
            }
            DB_ENABLED = True
    else:
        DB_ENABLED = False


def init_config():
    if os.path.exists(CONFIG_FILE):
        resp = input("config.json exists. Overwrite? (y/n): ").strip().lower()
        if resp not in ("yes", "y"):
            return
    with open(CONFIG_FILE, 'w') as f:
        json.dump(CONFIG_TEMPLATE, f, indent=2)
    print(f"Created {CONFIG_FILE}\nFill in credentials, then run: python3 {sys.argv[0]}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return new_state()


def new_state():
    return {
        "imported_threads": {},
        "collections": {},
        "folder_docs": {},
        "cache": {
            "spaces": None,         # folder tree from Phase 1
            "thread_data": None,    # {thread_id: {title, created_usec, ...}} from Phase 2 (without html)
            "user_names": None,     # {quip_user_id: name}
        },
    }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def load_mapping():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            return json.load(f)
    return {}


def save_mapping(mapping):
    with open(MAPPING_FILE, 'w') as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)


# --- Quip API ---
#
# Rate limits (from headers):
#   X-Ratelimit-Limit: 50          per-user, per minute
#   X-Ratelimit-Remaining: N       requests left in window
#   X-Ratelimit-Reset: timestamp   when the window resets
#   X-Company-Ratelimit-Limit: 600 per-company, per minute
#   X-Company-Ratelimit-Remaining: N
#   Retry-After: seconds           seconds until reset

import threading

class QuipRateLimiter:
    """Rate limiter driven by actual Quip API response headers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.remaining = 50       # user limit
        self.company_remaining = 600
        self.reset_time = 0       # monotonic time when limits reset
        self.min_interval = 0.1   # minimum gap between requests
        self.last_request = 0

    def wait(self):
        with self.lock:
            now = time.monotonic()

            # If we've passed the reset window, limits are refreshed
            if now >= self.reset_time:
                self.remaining = 50
                self.company_remaining = 600

            # If out of quota, sleep until reset
            if self.remaining <= 1 or self.company_remaining <= 1:
                wait = self.reset_time - now
                if wait > 0:
                    print(f"      Rate limit: waiting {wait:.0f}s for reset...")
                    time.sleep(wait + 0.5)
                self.remaining = 50
                self.company_remaining = 600

            # Enforce minimum interval
            elapsed = time.monotonic() - self.last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            self.last_request = time.monotonic()

    def update_from_headers(self, headers):
        """Update limits from Quip response headers."""
        with self.lock:
            remaining = headers.get("X-Ratelimit-Remaining")
            if remaining is not None:
                self.remaining = int(remaining)

            company_remaining = headers.get("X-Company-Ratelimit-Remaining")
            if company_remaining is not None:
                self.company_remaining = int(company_remaining)

            reset_ts = headers.get("X-Ratelimit-Reset")
            if reset_ts:
                # Convert absolute timestamp to monotonic
                wall_now = time.time()
                mono_now = time.monotonic()
                self.reset_time = mono_now + (float(reset_ts) - wall_now)

    def on_throttle(self, resp_headers):
        """Called on 429/503. Use Retry-After header."""
        with self.lock:
            retry_after = resp_headers.get("Retry-After")
            if retry_after:
                wait = float(retry_after) + 1
            else:
                reset_ts = resp_headers.get("X-Ratelimit-Reset")
                if reset_ts:
                    wait = float(reset_ts) - time.time() + 1
                else:
                    wait = 10
            self.reset_time = time.monotonic() + max(wait, 1)
            self.remaining = 0
            return max(wait, 1)


_quip_limiter = QuipRateLimiter()


def quip_get(endpoint, retries=5):
    """GET from Quip API with header-driven rate limiting."""
    url = f"https://platform.quip.com/1/{endpoint}"
    headers = {"Authorization": f"Bearer {QUIP_TOKEN}"}
    for attempt in range(retries):
        _quip_limiter.wait()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                _quip_limiter.update_from_headers(resp.headers)
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                wait = _quip_limiter.on_throttle(e.headers)
                print(f"      Quip {e.code}, waiting {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def quip_get_blob(thread_id, blob_id):
    """Download blob bytes from Quip. Returns (bytes, content_type) or None."""
    url = f"https://platform.quip.com/1/blob/{thread_id}/{blob_id}"
    headers = {"Authorization": f"Bearer {QUIP_TOKEN}"}
    _quip_limiter.wait()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            _quip_limiter.update_from_headers(resp.headers)
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            return resp.read(), ct
    except Exception:
        return None


# --- Outline API ---

def outline_post(endpoint, data=None, retries=5):
    """POST JSON to Outline API."""
    url = f"{OUTLINE_URL}/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {OUTLINE_TOKEN}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data or {}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) + 1 if retry_after else 3 * (2 ** attempt)
                time.sleep(min(wait, 60))
            else:
                try:
                    body_text = e.read().decode("utf-8", errors="replace")[:500]
                    print(f"      Outline {e.code} on {endpoint}: {body_text}")
                except Exception:
                    pass
                raise


def outline_upload(html_bytes, filename, collection_id, parent_doc_id=None, retries=5):
    """Multipart upload HTML to Outline documents.import."""
    boundary = "----MigrateBoundary7MA4YWxk"
    body = b""
    fields = {"collectionId": collection_id, "publish": "true"}
    if parent_doc_id:
        fields["parentDocumentId"] = parent_doc_id
    for key, val in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: text/html\r\n\r\n"
    body += html_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"{OUTLINE_URL}/api/documents.import"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {OUTLINE_TOKEN}")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, OSError) as e:
            is_retryable = (isinstance(e, urllib.error.HTTPError) and e.code == 429) or "Broken pipe" in str(e)
            if is_retryable and attempt < retries - 1:
                time.sleep(3 * (2 ** attempt))
            else:
                raise


# --- Quip folder walk ---

def walk_quip_folders(state):
    """Walk all Quip folders. Returns {space_name: FolderNode tree}. Uses cache if available."""
    print("=" * 50)
    print("Phase 1: Walking Quip folder tree")
    print("=" * 50)

    cached = state.get("cache", {}).get("spaces")
    if cached:
        # Count threads from cache
        def count_threads(node):
            n = len(node["thread_ids"])
            for sub in node["subfolders"].values():
                n += count_threads(sub)
            return n
        total = sum(count_threads(s) for s in cached.values())
        print(f"  [cached] {total} threads in {len(cached)} spaces")
        return cached

    user = quip_get("users/current")
    root_ids = user.get("shared_folder_ids", []) + user.get("group_folder_ids", [])
    print(f"  Root folders: {len(root_ids)}")

    all_threads = {}   # thread_id -> {title, created_usec, updated_usec, author_id}
    tree = {}          # folder_id -> {title, thread_ids, subfolders: {folder_id: ...}}
    visited = set()

    def walk(folder_id, depth=0):
        if folder_id in visited:
            return None
        visited.add(folder_id)
        try:
            data = quip_get(f"folders/{folder_id}")
        except Exception as e:
            print(f"    Warning: cannot read folder {folder_id}: {e}")
            return None

        folder = data.get("folder", {})
        title = folder.get("title", folder_id)
        children = data.get("children", [])
        member_ids = data.get("member_ids", []) or []

        thread_ids = [c["thread_id"] for c in children if "thread_id" in c]
        subfolder_ids = [c["folder_id"] for c in children if "folder_id" in c]

        indent = "  " * (depth + 1)
        members_str = f", {len(member_ids)} members" if member_ids else ""
        print(f"{indent}{title}: {len(thread_ids)} docs, {len(subfolder_ids)} folders{members_str}")

        subfolders = {}
        for fid in subfolder_ids:
            sub = walk(fid, depth + 1)
            if sub:
                subfolders[fid] = sub

        node = {"title": title, "thread_ids": thread_ids, "subfolders": subfolders, "member_ids": member_ids}
        tree[folder_id] = node
        return node

    spaces = {}
    for fid in root_ids:
        node = walk(fid)
        if node:
            spaces[fid] = node

    # Count totals
    def count_threads(node):
        n = len(node["thread_ids"])
        for sub in node["subfolders"].values():
            n += count_threads(sub)
        return n

    total = sum(count_threads(s) for s in spaces.values())
    print(f"\n  Total: {total} threads in {len(visited)} folders")

    # Cache folder tree
    state.setdefault("cache", {})["spaces"] = spaces
    save_state(state)

    return spaces


# --- Batch fetch thread metadata ---

def fetch_thread_data(spaces, state):
    """Batch fetch metadata + HTML for all threads in one pass.

    Caches metadata and user_names in state. HTML is only fetched for new threads.

    Returns (thread_data, user_names):
      thread_data: {thread_id: {title, created_usec, updated_usec, author_id, author_name, html}}
      user_names: {quip_user_id: name}
    """
    print("\n" + "=" * 50)
    print("Phase 2: Fetching thread data (metadata + HTML)")
    print("=" * 50)

    cache = state.setdefault("cache", {})
    cached_threads = cache.get("thread_data") or {}  # metadata without html
    cached_user_names = cache.get("user_names") or {}

    # Collect all thread IDs and folder member IDs
    all_ids = set()
    all_member_ids = set()

    def collect(node):
        all_ids.update(node["thread_ids"])
        all_member_ids.update(node.get("member_ids", []))
        for sub in node["subfolders"].values():
            collect(sub)

    for space in spaces.values():
        collect(space)

    # Determine which threads need fetching:
    # - already imported → skip entirely (use state)
    # - metadata cached but not imported → need HTML only (re-fetch)
    # - not cached at all → need full fetch
    imported_ids = set(state["imported_threads"].keys())
    cached_ids = set(cached_threads.keys())
    new_ids = all_ids - imported_ids - cached_ids   # need full fetch
    need_html_ids = (all_ids - imported_ids) & cached_ids  # have metadata, need HTML

    print(f"  Total: {len(all_ids)}, imported: {len(all_ids & imported_ids)}, "
          f"cached: {len(need_html_ids)}, new: {len(new_ids)}")

    threads = {}
    author_ids = set()

    # Fetch new threads (metadata + HTML in one batch call)
    ids_to_fetch = list(new_ids | need_html_ids)
    if ids_to_fetch:
        batch_size = 6
        total_batches = (len(ids_to_fetch) + batch_size - 1) // batch_size
        for i in range(0, len(ids_to_fetch), batch_size):
            batch = ids_to_fetch[i:i + batch_size]
            batch_num = i // batch_size + 1
            print(f"\r    Batch {batch_num}/{total_batches} ({len(threads)} fetched)", end="", flush=True)
            try:
                result = quip_get(f"threads/?ids={','.join(batch)}")
                for tid, tdata in result.items():
                    t = tdata.get("thread", {})
                    meta = {
                        "title": t.get("title", "").strip(),
                        "created_usec": t.get("created_usec"),
                        "updated_usec": t.get("updated_usec"),
                        "author_id": t.get("author_id"),
                    }
                    threads[tid] = {**meta, "html": tdata.get("html", "")}
                    # Cache metadata (without html — too large)
                    cached_threads[tid] = meta
                    if t.get("author_id"):
                        author_ids.add(t["author_id"])
            except Exception as e:
                print(f"\n    Warning: batch failed: {e}")
        print()
    else:
        print("  All threads already fetched")

    # Add metadata for already-imported threads (for DB update phase)
    for tid in (all_ids & imported_ids):
        if tid not in threads:
            threads[tid] = cached_threads.get(tid) or state["imported_threads"].get(tid, {})

    # Include folder members in user resolution (skip if no users/permissions)
    if not OPT_NO_USERS:
        author_ids.update(all_member_ids)

    # Resolve user names — only fetch unknown ones
    new_author_ids = author_ids - set(cached_user_names.keys())
    if new_author_ids:
        print(f"  Resolving {len(new_author_ids)} new user names ({len(cached_user_names)} cached)...")
        batch_size = 6
        uid_list = list(new_author_ids)
        for i in range(0, len(uid_list), batch_size):
            batch = uid_list[i:i + batch_size]
            try:
                result = quip_get(f"users/?ids={','.join(batch)}")
                for uid, udata in result.items():
                    cached_user_names[uid] = udata.get("name", "")
            except Exception:
                pass
    else:
        print(f"  User names: {len(cached_user_names)} (all cached)")

    user_names = cached_user_names

    # Attach author names to thread data
    for meta in threads.values():
        aid = meta.get("author_id")
        if aid and aid in user_names:
            meta["author_name"] = user_names[aid]

    # Save cache
    cache["thread_data"] = cached_threads
    cache["user_names"] = cached_user_names
    save_state(state)

    print(f"  Done: {len(threads)} threads, {len(user_names)} authors")
    return threads, user_names


# --- Author mapping ---

def create_author_mapping(user_names):
    """Create/update Outline users for all Quip authors."""
    print("\n" + "=" * 50)
    print("Phase 3: Author mapping")
    print("=" * 50)

    # Get existing Outline users
    result = outline_post("users.list", {"limit": 100})
    outline_users = {}
    for u in result.get("data", []):
        outline_users[u["name"].lower()] = (u["id"], u["name"], u.get("email", ""))
    print(f"  Existing Outline users: {len(outline_users)}")

    existing = load_mapping()
    mapping = {}
    created = 0

    all_names = set(n for n in user_names.values() if n.strip())

    for name in sorted(all_names):
        if not name.strip():
            continue

        # Reuse existing mapping
        if name in existing and existing[name] is not None:
            mapping[name] = existing[name]
            print(f"  [existing] {name}")
            continue

        # Match by name parts
        matched = None
        for part in name.lower().split():
            if len(part) < 3:
                continue
            for oname, (oid, _, _) in outline_users.items():
                if part in oname:
                    matched = oid
                    break
            if matched:
                break
        if matched:
            mapping[name] = matched
            print(f"  [matched]  {name} -> {matched}")
            continue

        # Create new user
        email_name = transliterate(name).lower().replace(" ", ".").strip(".")
        email_name = re.sub(r'[^a-z0-9.]', '', email_name) or "user"
        email = f"{email_name}@imported.local"
        suffix = 0
        while any(e == email for _, (_, _, e) in outline_users.items()):
            suffix += 1
            email = f"{email_name}{suffix}@imported.local"

        try:
            result = outline_post("users.invite", {
                "invites": [{"email": email, "name": name, "role": "member"}],
            })
            new_id = result["data"]["users"][0]["id"]
            # Activate user so they appear in Outline admin UI
            if DB_ENABLED:
                try:
                    conn = psycopg2.connect(**DB_CONFIG)
                    cur = conn.cursor()
                    cur.execute('UPDATE users SET "lastActiveAt" = "createdAt" WHERE id = %s::uuid', (new_id,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            mapping[name] = new_id
            outline_users[name.lower()] = (new_id, name, email)
            created += 1
            print(f"  [created]  {name} ({email})")
        except Exception as e:
            print(f"  [error]    {name}: {e}")
            mapping[name] = None

    save_mapping(mapping)
    mapped = sum(1 for v in mapping.values() if v is not None)
    print(f"\n  Total: {len(mapping)} authors, {mapped} mapped, {created} created")
    return mapping


# --- Outline attachments ---

def outline_upload_attachment(blob_bytes, filename, content_type):
    """Upload a blob as an Outline attachment. Returns attachment URL or None."""
    try:
        # Step 1: create attachment record
        result = outline_post("attachments.create", {
            "name": filename,
            "contentType": content_type,
            "size": len(blob_bytes),
        })
        upload_url = result["data"]["uploadUrl"]
        form = result["data"]["form"]
        attachment_url = result["data"]["attachment"]["url"]

        # Step 2: upload file
        boundary = "----AttachUploadBoundary"
        body = b""
        for k, v in form.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n".encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += blob_bytes
        body += f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(f"{OUTLINE_URL}{upload_url}", data=body, method="POST")
        req.add_header("Authorization", f"Bearer {OUTLINE_TOKEN}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req) as resp:
            pass  # 200 = success

        return attachment_url
    except Exception:
        return None


# --- Progress tracking ---

class Progress:
    """Tracks migration progress with ETA calculation."""

    def __init__(self, total):
        self.total = total
        self.done = 0
        self.skipped = 0
        self.errors = 0
        self.start_time = time.monotonic()

    def _prefix(self):
        processed = self.done + self.skipped + self.errors
        pct = processed * 100 // self.total if self.total else 0
        return f"[{processed}/{self.total} {pct}%]"

    def log_imported(self, title, extras=""):
        self.done += 1
        print(f"  {self._prefix()} [+] {title}{extras}")

    def log_skipped(self, title, reason=""):
        self.skipped += 1
        r = f" ({reason})" if reason else ""
        print(f"  {self._prefix()} [skip] {title}{r}")

    def log_error(self, title, error):
        self.errors += 1
        print(f"  {self._prefix()} [ERROR] {title}: {error}")

    def log_folder(self, title):
        print(f"  {self._prefix()} [folder] {title}/")

    def summary(self):
        elapsed = time.monotonic() - self.start_time
        if elapsed < 60:
            time_str = f"{elapsed:.0f}s"
        else:
            time_str = f"{elapsed / 60:.1f}m"
        print(f"\n  Done in {time_str}: {self.done} imported, {self.skipped} skipped, {self.errors} errors")


# Global progress instance, set in main()
progress = None


# --- Process single thread ---

def process_thread(thread_id, collection_id, parent_doc_id, thread_data, author_mapping, user_names, state):
    """Process a thread: download blobs, import into Outline, create comments.

    thread_data must contain 'html' (fetched in Phase 2).
    Returns (outline_doc_id, comment_data_list) or None on error.
    """
    if thread_id in state["imported_threads"]:
        title = state["imported_threads"][thread_id].get("title", thread_id)
        if progress:
            progress.log_skipped(title)
        return None

    title = thread_data.get("title", thread_id)
    html = thread_data.get("html", "")

    if not html:
        if progress:
            progress.log_skipped(title, "no HTML")
        return None

    try:
        # 1. Find blob references (deduplicate)
        blob_refs = list(dict.fromkeys(re.findall(r'/blob/([^/]+)/([^"\'?\s<>]+)', html)))
        if blob_refs and not OPT_NO_ATTACHMENTS:
            # 2. Download blobs in parallel from Quip
            blobs = {}
            with ThreadPoolExecutor(max_workers=BLOB_CONCURRENCY) as pool:
                futures = {
                    pool.submit(quip_get_blob, tid, bid): (tid, bid)
                    for tid, bid in blob_refs
                }
                for future in as_completed(futures):
                    tid, bid = futures[future]
                    try:
                        result = future.result()
                        if result:
                            blobs[(tid, bid)] = result
                    except Exception:
                        pass

            # 3. Upload as Outline attachments and replace URLs
            for (tid, bid), (blob_bytes, content_type) in blobs.items():
                ext = mimetypes.guess_extension(content_type) or ".bin"
                filename = f"{bid[:12]}{ext}"
                att_url = outline_upload_attachment(blob_bytes, filename, content_type)
                if att_url:
                    html = html.replace(f"/blob/{tid}/{bid}", att_url)
                else:
                    # Fallback: small blobs inline, large ones skip
                    if len(blob_bytes) < 500_000:
                        b64 = base64.b64encode(blob_bytes).decode()
                        html = html.replace(f"/blob/{tid}/{bid}", f"data:{content_type};base64,{b64}")
                    else:
                        html = html.replace(f"/blob/{tid}/{bid}", "#")

        # 4. Wrap in full HTML document for Outline import
        full_html = f"<html><head><title>{title}</title></head><body>{html}</body></html>"

        # 5. Upload to Outline
        result = outline_upload(
            full_html.encode("utf-8"),
            f"{title[:100]}.html",
            collection_id,
            parent_doc_id,
        )
        doc_id = result["data"]["id"]

        # 6. Fetch and create comments via Messages API (1 request per thread)
        comment_data = []
        if not OPT_NO_COMMENTS:
            try:
                messages = quip_get(f"messages/{thread_id}")
                if messages:
                    messages.sort(key=lambda m: m.get("created_usec", 0))
                    for msg in messages:
                        if not msg.get("text"):
                            continue
                        author_qid = msg.get("author_id", "")
                        author_name = user_names.get(author_qid, "")
                        created_usec = msg.get("created_usec")

                        if OPT_NO_USERS:
                            # No user mapping — show author name and time in text
                            date_str = ""
                            if created_usec:
                                dt = datetime.fromtimestamp(created_usec / 1e6, tz=timezone.utc)
                                date_str = dt.strftime("%d %b %Y %H:%M")
                            prefix = f"[{author_name or 'Unknown'}, {date_str}]" if date_str else f"[{author_name or 'Unknown'}]"
                            msg_text = f"{prefix}\n{msg['text']}"
                        else:
                            outline_uid = author_mapping.get(author_name) if author_name else None
                            msg_text = msg["text"] if outline_uid else f"[{author_name or 'Unknown'}]\n{msg['text']}"

                        # Extract annotated (quoted) text from HTML
                        ann_id = msg.get("annotation", {}).get("id", "")
                        annotated_text = ""
                        if ann_id:
                            ann_match = re.search(
                                f'<annotation[^>]*id="{re.escape(ann_id)}"[^>]*>(.*?)</annotation>',
                                html, re.DOTALL,
                            )
                            if ann_match:
                                annotated_text = re.sub(r'<[^>]+>', '', ann_match.group(1)).strip()[:200]

                        # Build ProseMirror content with optional quote
                        pm_content = []
                        if annotated_text:
                            pm_content.append({
                                "type": "paragraph",
                                "content": [{"type": "text", "text": f"> {annotated_text}", "marks": [{"type": "em"}]}],
                            })
                        pm_content.append({
                            "type": "paragraph",
                            "content": [{"type": "text", "text": msg_text}],
                        })

                        try:
                            cresult = outline_post("comments.create", {
                                "documentId": doc_id,
                                "data": {"type": "doc", "content": pm_content},
                            })
                            comment_data.append({
                                "comment_id": cresult["data"]["id"],
                                "created_usec": created_usec,
                                "author_id": author_qid,
                            })
                        except Exception as e:
                            print(f"      Comment error: {e}")
            except Exception:
                pass  # No messages

        blob_count = len(blob_refs) if blob_refs else 0
        comment_count = len(comment_data)
        extras = []
        if blob_count:
            extras.append(f"{blob_count} images")
        if comment_count:
            extras.append(f"{comment_count} comments")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        if progress:
            progress.log_imported(title, extra_str)

        # Track in state
        state["imported_threads"][thread_id] = {
            "title": title,
            "doc_id": doc_id,
            "comments": comment_data,
        }

        return doc_id, comment_data

    except Exception as e:
        if progress:
            progress.log_error(title, e)
        return None


# --- Import folder tree ---

def import_folder(node, collection_id, parent_doc_id, thread_data_map, author_mapping, user_names, state, depth=0):
    """Recursively import a folder's threads and subfolders."""
    imported = 0
    errors = 0

    # Import threads in this folder
    for tid in node["thread_ids"]:
        tdata = thread_data_map.get(tid, {})
        result = process_thread(tid, collection_id, parent_doc_id, tdata, author_mapping, user_names, state)
        if result:
            imported += 1
            # Save state after each doc for crash recovery
            if imported % 5 == 0:
                save_state(state)
        elif result is None and tid not in state["imported_threads"]:
            errors += 1

    # Import subfolders
    for fid, sub_node in node["subfolders"].items():
        folder_title = sub_node["title"]
        if progress:
            progress.log_folder(folder_title)

        # Create or reuse folder parent doc
        folder_doc_key = f"{collection_id}:{fid}"
        if folder_doc_key in state["folder_docs"]:
            folder_doc_id = state["folder_docs"][folder_doc_key]
        else:
            result = outline_post("documents.create", {
                "title": folder_title,
                "collectionId": collection_id,
                "text": "",
                "publish": True,
                "parentDocumentId": parent_doc_id,
            })
            folder_doc_id = result["data"]["id"]
            state["folder_docs"][folder_doc_key] = folder_doc_id

        sub_imported, sub_errors = import_folder(
            sub_node, collection_id, folder_doc_id,
            thread_data_map, author_mapping, user_names, state, depth + 1,
        )
        imported += sub_imported
        errors += sub_errors

    return imported, errors


# --- Permissions ---

QUIP_TO_OUTLINE_PERMISSION = {
    "OWN": "read_write",
    "EDIT": "read_write",
    "VIEW": "read",
}


def sync_collection_permissions(collection_id, folder_node, user_names, author_mapping):
    """Set Outline collection permissions based on Quip folder member_ids.

    Collects member_ids from the folder and all subfolders,
    resolves to Outline user IDs via author_mapping, and grants access.
    """
    # Collect all unique Quip user IDs from this folder tree
    quip_member_ids = set()

    def collect_members(node):
        quip_member_ids.update(node.get("member_ids", []))
        for sub in node.get("subfolders", {}).values():
            collect_members(sub)

    collect_members(folder_node)

    if not quip_member_ids:
        return 0

    granted = 0
    for quid in quip_member_ids:
        name = user_names.get(quid)
        if not name:
            continue
        outline_uid = author_mapping.get(name)
        if not outline_uid:
            continue
        try:
            outline_post("collections.add_user", {
                "id": collection_id,
                "userId": outline_uid,
                "permission": "read_write",
            })
            granted += 1
        except Exception:
            pass  # Already a member or other error

    return granted


# --- Update DB ---

def update_db(state, thread_meta_map, user_names, author_mapping):
    """Update timestamps and authors in Outline DB."""
    if not DB_ENABLED:
        print("\n  Skipping DB update (no database configured)")
        return

    print("\n" + "=" * 50)
    print("Phase 5: Updating timestamps and authors in DB")
    print("=" * 50)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Update documents
    doc_updated = 0
    for tid, info in state["imported_threads"].items():
        meta = thread_meta_map.get(tid)
        if not meta:
            continue
        doc_id = info["doc_id"]
        created_usec = meta.get("created_usec")
        updated_usec = meta.get("updated_usec")
        if not created_usec or not updated_usec:
            continue

        created_at = datetime.fromtimestamp(created_usec / 1e6, tz=timezone.utc)
        updated_at = datetime.fromtimestamp(updated_usec / 1e6, tz=timezone.utc)

        # Resolve author
        author_name = meta.get("author_name")
        outline_user_id = author_mapping.get(author_name) if author_name else None

        if outline_user_id:
            cur.execute(
                'UPDATE documents SET "createdAt" = %s, "updatedAt" = %s, '
                '"createdById" = %s::uuid, "lastModifiedById" = %s::uuid '
                'WHERE id = %s::uuid',
                (created_at, updated_at, outline_user_id, outline_user_id, doc_id),
            )
        else:
            cur.execute(
                'UPDATE documents SET "createdAt" = %s, "updatedAt" = %s WHERE id = %s::uuid',
                (created_at, updated_at, doc_id),
            )
        doc_updated += 1

    # Update comments (skip if noUsers — date already in comment text)
    comment_updated = 0
    if not OPT_NO_USERS:
        for tid, info in state["imported_threads"].items():
            for cdata in info.get("comments", []):
                comment_id = cdata.get("comment_id")
                if not comment_id:
                    continue
                created_usec = cdata.get("created_usec")
                if not created_usec:
                    continue

                created_at = datetime.fromtimestamp(created_usec / 1e6, tz=timezone.utc)
                author_qid = cdata.get("author_id")
                author_name = user_names.get(author_qid)
                outline_user_id = author_mapping.get(author_name) if author_name else None

                if outline_user_id:
                    cur.execute(
                        'UPDATE comments SET "createdAt" = %s, "createdById" = %s::uuid WHERE id = %s::uuid',
                        (created_at, outline_user_id, comment_id),
                    )
                else:
                    cur.execute(
                        'UPDATE comments SET "createdAt" = %s WHERE id = %s::uuid',
                        (created_at, comment_id),
                    )
                comment_updated += 1

    conn.commit()
    conn.close()
    print(f"  Documents updated: {doc_updated}")
    print(f"  Comments updated: {comment_updated}")


# --- Folder filter ---

def filter_spaces(spaces, folder_names):
    """Filter spaces tree to only include matching folders.

    Matches against subfolder titles (case-insensitive).
    The space (root) is kept if any of its subfolders match.
    Top-level threads in the space are excluded unless the space title itself matches.
    """
    folder_names_lower = {n.lower() for n in folder_names}
    filtered = {}

    for fid, space in spaces.items():
        space_title_lower = space["title"].lower()

        # Check if the space itself matches
        if space_title_lower in folder_names_lower:
            filtered[fid] = space
            continue

        # Filter subfolders
        matched_subfolders = {}
        for sub_fid, sub_node in space["subfolders"].items():
            if _folder_matches(sub_node, folder_names_lower):
                matched_subfolders[sub_fid] = sub_node

        if matched_subfolders:
            # Keep space but only with matched subfolders (no top-level threads)
            filtered[fid] = {
                **space,
                "thread_ids": [],
                "subfolders": matched_subfolders,
            }

    # Print what matched
    def count_threads(node):
        n = len(node["thread_ids"])
        for sub in node["subfolders"].values():
            n += count_threads(sub)
        return n

    total = sum(count_threads(s) for s in filtered.values())
    matched_names = set()
    for s in filtered.values():
        for sub in s["subfolders"].values():
            matched_names.add(sub["title"])
    print(f"  Filtered: {len(matched_names)} folders, {total} threads")
    if matched_names:
        for name in sorted(matched_names):
            print(f"    - {name}")

    return filtered


def _folder_matches(node, names_lower):
    """Check if folder or any of its children match the filter."""
    if node["title"].lower() in names_lower:
        return True
    for sub in node["subfolders"].values():
        if _folder_matches(sub, names_lower):
            return True
    return False


# --- Main ---

def main():
    state = load_state()

    # Ensure cache structure exists (for older state files)
    if "cache" not in state:
        state["cache"] = {"spaces": None, "thread_data": None, "user_names": None}

    # Phase 1: Walk Quip folders (full tree, cached)
    spaces = walk_quip_folders(state)
    if not spaces:
        print("No folders found.")
        return

    # Apply --folders filter (on cached full tree)
    if OPT_FOLDERS:
        spaces = filter_spaces(spaces, OPT_FOLDERS)
        if not spaces:
            print(f"No folders matched: {', '.join(OPT_FOLDERS)}")
            return

    # Phase 2: Fetch thread data (metadata + HTML in one pass)
    thread_data_map, user_names = fetch_thread_data(spaces, state)

    # Phase 3: Author mapping
    if OPT_NO_USERS:
        author_mapping = {}
        print("\n  Skipping user creation (--noUsers)")
    else:
        author_mapping = create_author_mapping(user_names)

    # Phase 4: Import
    print("\n" + "=" * 50)
    print("Phase 4: Importing into Outline")
    print("=" * 50)

    # Count total threads for progress
    global progress
    def count_all(node):
        n = len(node["thread_ids"])
        for sub in node["subfolders"].values():
            n += count_all(sub)
        return n
    total_threads = sum(count_all(s) for s in spaces.values())
    progress = Progress(total_threads)

    colors = ["#4B9EFF", "#FF6B6B", "#50C878", "#FF8C00", "#9370DB", "#20B2AA"]
    total_imported = 0
    total_errors = 0

    for i, (fid, space) in enumerate(spaces.items()):
        space_name = space["title"]
        print(f"\n  === {space_name} ===")

        # Create or reuse collection
        if fid in state["collections"]:
            coll_id = state["collections"][fid]
            print(f"  Reusing collection: {coll_id}")
        else:
            result = outline_post("collections.create", {
                "name": space_name,
                "color": colors[i % len(colors)],
            })
            coll_id = result["data"]["id"]
            state["collections"][fid] = coll_id
            print(f"  Created collection: {coll_id}")

        # Set collection permissions from Quip folder members
        if not OPT_NO_PERMISSIONS:
            granted = sync_collection_permissions(coll_id, space, user_names, author_mapping)
            if granted:
                print(f"  Permissions: {granted} users granted access")

        # Import top-level threads (directly in space, no parent folder)
        for tid in space["thread_ids"]:
            tdata = thread_data_map.get(tid, {})
            result = process_thread(tid, coll_id, None, tdata, author_mapping, user_names, state)
            if result:
                total_imported += 1

        # Import subfolders
        for sub_fid, sub_node in space["subfolders"].items():
            folder_title = sub_node["title"]
            print(f"\n    --- {folder_title} ---")

            folder_doc_key = f"{coll_id}:{sub_fid}"
            if folder_doc_key in state["folder_docs"]:
                folder_doc_id = state["folder_docs"][folder_doc_key]
            else:
                result = outline_post("documents.create", {
                    "title": folder_title,
                    "collectionId": coll_id,
                    "text": "",
                    "publish": True,
                })
                folder_doc_id = result["data"]["id"]
                state["folder_docs"][folder_doc_key] = folder_doc_id

            imported, errors = import_folder(
                sub_node, coll_id, folder_doc_id,
                thread_data_map, author_mapping, user_names, state,
            )
            total_imported += imported
            total_errors += errors

        save_state(state)

    progress.summary()

    # Phase 5: Update DB
    update_db(state, thread_data_map, user_names, author_mapping)

    save_state(state)
    print(f"\n{'='*50}")
    print(f"All done! {progress.done} imported, {progress.skipped} skipped, {progress.errors} errors.")


def print_help():
    print("""
Usage: quip-to-outline [options]

Commands:
  --init              Generate config.json template
  --help              Show this help

Options:
  --noComments        Skip comment migration
  --noPermissions     Skip permission sync (collection access)
  --noAttachments     Skip image/file downloads (text only)
  --noUsers           Skip user creation (implies --noPermissions)
                      Comments will include author name + timestamp in text
  --resetCache        Clear cached Quip data, re-fetch everything
                      Import progress (already imported docs) is preserved
  --folders a,b,c     Only migrate specified folders (comma-separated names)
                      Full tree is cached; filter applied at runtime

Workflow:
  1. quip-to-outline --init
  2. Edit config.json with credentials
  3. quip-to-outline [options]

Examples:
  quip-to-outline                          Full migration
  quip-to-outline --noAttachments          Text only, no images
  quip-to-outline --noUsers --noComments   Minimal: docs only, no users/comments

Resume: safe to re-run, skips already imported documents (state.json).
""")


def parse_flags():
    global OPT_NO_COMMENTS, OPT_NO_PERMISSIONS, OPT_NO_ATTACHMENTS, OPT_NO_USERS, OPT_FOLDERS
    args_lower = [a.lower() for a in sys.argv]
    OPT_NO_COMMENTS = "--nocomments" in args_lower
    OPT_NO_PERMISSIONS = "--nopermissions" in args_lower
    OPT_NO_ATTACHMENTS = "--noattachments" in args_lower
    OPT_NO_USERS = "--nousers" in args_lower
    if OPT_NO_USERS:
        OPT_NO_PERMISSIONS = True

    # --folders name1,name2,name3
    for i, arg in enumerate(sys.argv):
        if arg.lower() == "--folders" and i + 1 < len(sys.argv):
            names = [n.strip() for n in sys.argv[i + 1].split(",") if n.strip()]
            OPT_FOLDERS = set(names)
            break

    # --resetCache: clear cached Quip data, keep import progress
    if "--resetcache" in args_lower:
        if os.path.exists(STATE_FILE):
            state = load_state()
            state["cache"] = {"spaces": None, "thread_data": None, "user_names": None}
            save_state(state)
        print("Cache cleared (import progress preserved)")

    flags = []
    if OPT_NO_COMMENTS:     flags.append("noComments")
    if OPT_NO_PERMISSIONS:  flags.append("noPermissions")
    if OPT_NO_ATTACHMENTS:  flags.append("noAttachments")
    if OPT_NO_USERS:        flags.append("noUsers")
    if OPT_FOLDERS:          flags.append(f"folders: {','.join(sorted(OPT_FOLDERS))}")
    if flags:
        print(f"Mode: {', '.join(flags)}")


def cli_main():
    """Entry point for the CLI."""
    if "--help" in sys.argv or "-h" in sys.argv:
        print_help()
    elif "--init" in sys.argv:
        init_config()
    else:
        cfg = load_config()
        setup_globals(cfg)
        parse_flags()
        main()


if __name__ == "__main__":
    cli_main()
