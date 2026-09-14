"""Content cleaning and HTML parsing — contract §5, §6 (Phase 6.3).

Four pieces: the parser boundary (``parse.py``, decision record B1), the
removal rules R1–R11 (``rules.py``, contract §5.3 — R12 and R13 remove
nothing by design), block construction into ``canonical.Block``
(``blocks.py``, contract §6, §9, §10, §11), and the unmarked-header
detector (``tables.py``, decision record B3), which reports one
``metadata.cleaning`` counter and builds no blocks.

The pipeline is pure and source-neutral: it takes markup and returns
blocks, and knows nothing about MySQL, HTTP or any adapter. Nothing here
calls it from a hash-bearing path yet — that arrives with the Blog
adapter, which must bump ``NORMALIZER_VERSION`` in the same change (B1 §4).
"""
