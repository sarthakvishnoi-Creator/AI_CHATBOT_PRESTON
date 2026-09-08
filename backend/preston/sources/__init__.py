"""Source-side database infrastructure — decision record B2.

Not a source adapter. This package holds the connection plumbing an
adapter needs (engine construction, read-only enforcement, the
async-to-sync isolation pattern) — never the allow-listed tables an
adapter reads or the extraction logic itself. Those are B4 and Phase 6.3
deliverables and are not built by this package.
"""
