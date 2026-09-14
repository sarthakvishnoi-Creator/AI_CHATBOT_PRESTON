"""The blog pre-clean gate — contract §15.2.

Runs on a **raw** blog row, before any HTML is parsed and long before
anything is hashed (§15.1):

    extract → PRE-CLEAN GATE → clean → POST-CLEAN GATE → hash → persist

Its whole purpose is to stop a broken extraction from reaching the hash.
Hashing a broken row and finding it "changed" is precisely how an empty
record overwrites good content, so a row rejected here becomes an
``ExtractionFailure`` and is never parsed: the stored blocks, chunks and
hash of that document remain the last known-good copy, its ``status`` is
unchanged, and only ``consecutive_failure_count`` moves (§15.4).

**Nothing here repairs anything.** A row is accepted as authored or
refused with a code; there is no defaulting, no trimming into validity and
no substitution. **Nothing here decides publication either** — ``status``
is not read, because it is not a publication gate (it holds ``''`` for 284
of 368 rows, and ``'1'``/``'1.0'``/``'0'``/``'0.0'`` for the rest).

**Which §15.2 checks apply to a blog, and which deliberately do not:**

* ``web_payload_refused`` — applied. §2.8: cleaning accepts ``mysql`` and
  ``api`` only. Website HTML is a visibility verdict, never a content
  source; a dead route renders a well-formed not-found shell that would
  pass every structural check in §15.
* ``placeholder_record`` — applied, as defence in depth. The ``'a'``
  signature belongs to ``subone_cmgsubpage``, which the B4 allow-list
  already excludes, so this gate is the second line and not the control.
* ``empty_body`` — applied. Measured on the raw value, because nothing has
  been parsed yet: a body of ``"<p></p>"`` is *not* empty at this stage and
  is correctly the post-clean gate's ``empty_after_cleaning`` case.
* ``below_floor`` — applied **only when a floor has been calibrated**. §22.3
  forbids inventing one: "a number invented before the corpus is measured
  either fires constantly or never fires, and in both cases it is ignored —
  which is worse than having no gate." The parameter therefore defaults to
  ``None``, meaning uncalibrated, meaning the check does not run.
* ``no_headings`` — **not applied.** §15.2 scopes it to families whose
  field map declares heading fields and says outright that "a blog body
  legitimately may have none".
* ``no_paragraphs`` — collapses into ``empty_body`` for this family, which
  has exactly one body-bearing field. A separate check would be the same
  test under a second name.
* ``yield_collapse`` — **not applied here.** It compares a document against
  *its own historical* character count, which requires reading what is
  already stored. An adapter has no database access by design, and giving
  it one to satisfy this check would put persistence knowledge inside the
  source boundary. It belongs to the run-level metrics of §22, not to a
  per-row gate.
"""

from typing import Final

from preston.canonical import SourceType
from preston.validation import ValidationFailure

# The §15.2 rejection codes, verbatim. Used as ``ValidationFailure.reason``
# so that rejections can be counted per code in the run report (§15.4).
EMPTY_BODY: Final = "empty_body"
BELOW_FLOOR: Final = "below_floor"
PLACEHOLDER_RECORD: Final = "placeholder_record"
WEB_PAYLOAD_REFUSED: Final = "web_payload_refused"

# The placeholder signature recorded in SoT §07/§08: a single ``a`` standing
# in for a slug or a title. Matched exactly rather than by a "looks like
# junk" heuristic, because a catch-all cannot be audited (§5.3 R13).
_PLACEHOLDER_VALUES: Final = frozenset({"a"})


def _is_placeholder(value: str | None) -> bool:
    """Return whether a field carries the recorded placeholder signature."""
    return value is not None and value.strip().casefold() in _PLACEHOLDER_VALUES


def check_blog_row(
    *,
    canonical_uri: str,
    source_type: SourceType,
    url: str | None,
    title: str | None,
    body: str | None,
    minimum_body_characters: int | None = None,
) -> ValidationFailure | None:
    """Return the first §15.2 failure for a raw blog row, or ``None``.

    ``canonical_uri`` identifies the row in the returned failure so the
    engine can attribute it to the right document; it is not itself
    validated here, because identity is the post-clean gate's concern.

    ``minimum_body_characters`` is the family floor. Leave it ``None``
    until a calibration run has produced one (§22.3); the check is then
    simply not performed, which is the honest behaviour for a threshold
    nobody has measured yet.

    First failure rather than all failures: the row is refused either way,
    and one stable code is more useful in a run report than a list that
    shifts as unrelated checks are added.
    """
    # §2.8 — a web payload is a typed rejection, not an input. Checked
    # first because it disqualifies the record regardless of its content.
    if source_type == "web":
        return ValidationFailure(canonical_uri, WEB_PAYLOAD_REFUSED)

    if _is_placeholder(url) or _is_placeholder(title):
        return ValidationFailure(canonical_uri, PLACEHOLDER_RECORD)

    # Measured on the raw value: nothing has been parsed, so this asks
    # only whether the field carries anything at all.
    if body is None or not body.strip():
        return ValidationFailure(canonical_uri, EMPTY_BODY)

    if minimum_body_characters is not None and len(body) < minimum_body_characters:
        return ValidationFailure(canonical_uri, BELOW_FLOOR)

    return None
