"""Content cleaning and HTML parsing — contract §5, §6 (Phase 6.3).

Not yet a pipeline. Two pieces exist: the parser boundary (``parse.py``,
decision record B1) and the unmarked-header detector (``tables.py``,
decision record B3), which reports one ``metadata.cleaning`` counter and
builds no blocks. Block construction, the removal rules R1–R11 and
entity-safe assembly into ``canonical.Block`` are 6.3 deliverables and are
not built by this module.
"""
