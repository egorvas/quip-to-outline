#!/usr/bin/env python3
"""Scan msg_cache and report comments longer than 1000 characters."""

import json
import os
import sys

LIMIT = 1000

def main():
    cache_dir = sys.argv[1] if len(sys.argv) > 1 else "msg_cache"
    if not os.path.isdir(cache_dir):
        print(f"Directory not found: {cache_dir}")
        print(f"Usage: python3 {sys.argv[0]} /path/to/msg_cache")
        sys.exit(1)

    total_comments = 0
    long_comments = 0
    longest = 0
    longest_file = ""
    length_buckets = {1000: 0, 2000: 0, 5000: 0, 10000: 0}

    for fname in os.listdir(cache_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(cache_dir, fname)
        try:
            with open(fpath) as f:
                messages = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(messages, list):
            continue

        for msg in messages:
            text = msg.get("text", "")
            if not text:
                continue
            total_comments += 1
            length = len(text)
            if length > LIMIT:
                long_comments += 1
                for bucket in sorted(length_buckets.keys()):
                    if length > bucket:
                        length_buckets[bucket] += 1
                if length > longest:
                    longest = length
                    longest_file = fname
                thread_id = fname.replace(".json", "")
                preview = text[:200].replace("\n", "\\n")
                print(f"  [{length} chars] thread={thread_id}: {preview}...")

    print(f"Total comments:  {total_comments}")
    print(f"Over {LIMIT} chars: {long_comments} ({long_comments * 100 // max(total_comments, 1)}%)")
    print()
    print("Breakdown:")
    for bucket in sorted(length_buckets.keys()):
        print(f"  > {bucket:>5} chars: {length_buckets[bucket]}")
    print()
    print(f"Longest: {longest} chars (in {longest_file})")


if __name__ == "__main__":
    main()
