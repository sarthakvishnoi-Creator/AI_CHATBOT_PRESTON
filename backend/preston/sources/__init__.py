"""Source-side database infrastructure — decision record B2.

This package holds the connection plumbing an adapter needs (engine
construction, read-only enforcement, the async-to-sync isolation pattern
in ``mysql.py``), the source-neutral boundary types every adapter
satisfies (``contract.py``), the Blog table and column allow-list
(``blog_tables.py``, decision record B4) and the Blog pre-clean gate
(``blog_precheck.py``, contract §15.2).

**The adapter itself is not built.** No extraction query exists, nothing
reads from the allow-listed tables, and no ingestion run has been
executed — that is Phase 6.3B-2 steps 3–9.
"""
