#!/usr/bin/env python3
"""
find_host_pages.py
Searches the existing `documents` table for candidate host pages for each
diagram in image_manifest.json, by matching the framework name against
document titles and canonical URIs.

This does NOT write anything. It only prints candidates for you to confirm.

Usage:
    export DATABASE_URL="postgresql://user:pass@localhost:5432/preston"
    python find_host_pages.py --manifest image_manifest.json

Output: image_host_candidates.json — one entry per image, with 0-3 candidate
documents each, for you to confirm/pick before we write the real mapping.
"""

import argparse
import json
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras

# Scopes most likely to carry these diagrams — searched first, but we fall
# back to searching everything if nothing turns up there.
PRIORITY_SCOPES = [
    "grc", "audit_assessment", "security_testing",
    "management_training", "professional_training", "resource_process",
]

# A few frameworks have short/ambiguous names (e.g. "PCI DSS") that need
# a tighter search term than the framework string itself would give.
SEARCH_TERM_OVERRIDES = {
    "CCPA": "California Consumer Privacy",
    "PIPEDA": "PIPEDA",
    "WCAG & ADA": "WCAG",
    "ISO/IEC 27701 (PIMS)": "27701",
    "ISO/IEC 42001:2023": "42001",
    "NIST SP 800-53": "800-53",
    "NIST SP 800-171": "800-171",
}


def search_term_for(framework: str) -> str:
    if framework in SEARCH_TERM_OVERRIDES:
        return SEARCH_TERM_OVERRIDES[framework]
    # Strip a trailing version/year like ":2023" or "v4.0.1" for a looser match
    return re.sub(r"[:\s]v?\d[\d.]*$", "", framework).strip()


def search(cur, term: str, scopes: list[str] | None):
    query = """
        SELECT id, canonical_uri, title, metadata->>'scope' AS scope
        FROM documents
        WHERE status = 'active'
          AND (title ILIKE %(pat)s OR canonical_uri ILIKE %(pat)s)
    """
    params = {"pat": f"%{term}%"}
    if scopes:
        query += " AND metadata->>'scope' = ANY(%(scopes)s)"
        params["scopes"] = scopes
    query += " ORDER BY title LIMIT 5"
    cur.execute(query, params)
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="image_manifest.json")
    parser.add_argument("--out", default="image_host_candidates.json")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))["images"]
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    results = {}
    for filename, entry in manifest.items():
        if entry.get("skip"):
            continue
        framework = entry.get("framework", "")
        term = search_term_for(framework)

        candidates = search(cur, term, PRIORITY_SCOPES)
        searched_all = False
        if not candidates:
            candidates = search(cur, term, None)  # fall back to whole corpus
            searched_all = True

        results[filename] = {
            "framework": framework,
            "search_term": term,
            "searched_all_scopes": searched_all,
            "candidates": [dict(c) for c in candidates],
        }

        status = "OK" if candidates else "NO MATCH"
        print(f"[{status:8}] {filename[:55]:55} ({framework}) -> "
              f"{len(candidates)} candidate(s)")
        for c in candidates:
            print(f"             {c['canonical_uri']}  [{c['scope']}]")

    Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {args.out}")

    no_match = [f for f, r in results.items() if not r["candidates"]]
    multi_match = [f for f, r in results.items() if len(r["candidates"]) > 1]
    if no_match:
        print(f"\n{len(no_match)} image(s) with NO candidate page — need a manual URL:")
        for f in no_match:
            print(f"  {f}")
    if multi_match:
        print(f"\n{len(multi_match)} image(s) with MULTIPLE candidates — need you to pick one:")
        for f in multi_match:
            print(f"  {f}")

    conn.close()


if __name__ == "__main__":
    main()
