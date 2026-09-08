"""The HTML5 fragment parser boundary — decision record B1.

This is the **only** module in the codebase permitted to import
``html5lib``. Every other module — the eventual cleaning pipeline,
adapters, normalization — receives a plain
``xml.etree.ElementTree.Element`` from :func:`parse_fragment` and never
touches the parser library directly. That is what makes a future parser
change (a version bump, or a different library if ``html5lib`` is ever
replaced) a change to exactly one module.

**Approved parser: ``html5lib==1.1``** (decision record, B1). Selected
over ``html5lib-modern``, ``tinyhtml5``, ``selectolax``, ``html5-parser``,
``lxml`` HTML mode, ``html.parser`` and regex stripping — see the decision
record for the comparison and the reasons each alternative was rejected.
Frozen at ``1.1`` deliberately: contract §17.2 requires the parser's
identity and version to be part of ``normalizer_version``, and a parser
that never changes cannot silently invalidate a corpus that has already
been hashed.

**Configuration is part of the decision, not an implementation detail** —
recorded here so a change to any of the three choices below is visible,
not buried in a call site:

* ``container="body"`` — the fragment context element. It determines
  insertion mode and therefore the parser's error-recovery behaviour,
  which contract §6.6 makes the normative rule for unbalanced or
  misnested markup.
* ``namespaceHTMLElements=False`` — HTML elements are read unprefixed.
  Foreign content (``svg``, ``math``) is namespaced by the HTML5 parsing
  algorithm regardless of this flag — verified empirically against this
  exact configuration, not assumed from documentation — which is what R3
  and the §5.2.1 ``svg`` first-occurrence counter key on.
* ``treebuilder="etree"`` — builds a standard-library
  ``xml.etree.ElementTree`` tree. Comment nodes are real tree nodes (the
  ``Comment`` factory as the element's tag), and every attribute is
  copied verbatim into ``.attrib`` with none filtered at parse time —
  both verified against html5lib's etree tree builder, and both required
  by contract §5.3 (R1, R5–R9) and §5.5, which read attributes a later
  removal rule discards (``on*`` handlers, inline ``style``, `hidden`).

**Entity decoding happens exactly once, inside this parser's tokenizer**
(contract §4.2). ``parse_fragment`` must never be followed by
``html.unescape`` or any equivalent normalization pass over the parsed
text: html5lib's tokenizer resolves character references during parsing,
so a second decode is precisely the failure contract §4.2 names —
``&amp;lt;script&amp;gt;`` becoming markup again. No decoding call exists
anywhere in this module; that absence is the guarantee, not an oversight.

**This module removes nothing.** Comments, ``script``, ``style``,
``iframe``, ``svg``, ``form``, ``figure``, ``hr``, ``th``, ``blockquote``,
``pre``, ``code`` and every attribute (``on*`` handlers and
``javascript:`` hrefs included) survive into the returned tree unchanged.
Removal is the cleaning stage's job (contract §5.3, rules R1–R11), applied
downstream against the tree this function returns — never here. A parser
boundary that also removed content would make R1–R11 unauditable: the
first-occurrence counters in §5.2.1 and the removal counters in §22 must
see what was present before a rule acted on it.

**Version tracking.** ``PARSER_NAME``, ``PARSER_VERSION`` and
``PARSER_CONFIG_VERSION`` below are the identity contract §17.2 requires.
Whichever change wires :func:`parse_fragment` into the cleaning pipeline
must fold this tuple into ``preston.normalization.NORMALIZER_VERSION`` —
bumping the pinned html5lib version, or any of the configuration choices
above, without also bumping ``NORMALIZER_VERSION`` breaks the
``REPROCESSED`` guarantee (contract §17, architecture §7.1). That wiring
does not exist yet: no cleaning pipeline is built (contract §5 is out of
scope for B1), so nothing currently reads these constants except tests.
"""

from typing import Final, cast
from xml.etree.ElementTree import Element

import html5lib

# The approved parser (decision record B1). The version is also enforced
# by the exact pin in pyproject.toml (``html5lib==1.1``); it is recorded
# again here because it is part of what ``normalizer_version`` must track,
# and that tracking must survive even if the pin string is ever read
# differently by tooling.
PARSER_NAME: Final = "html5lib"
PARSER_VERSION: Final = "1.1"

# Bump this when the parsing configuration below changes (container,
# namespace handling, treebuilder) independently of a html5lib version
# change — a config change and a library upgrade are different events,
# and either may happen without the other.
PARSER_CONFIG_VERSION: Final = 1

# The fragment context element. See the module docstring: this is a
# recovery-behaviour choice, not an arbitrary default.
_FRAGMENT_CONTAINER: Final = "body"


def parse_fragment(markup: str) -> Element:
    """Parse one HTML fragment into a standard-library element tree.

    ``markup`` is one rich-text field's raw value (contract §2.4:
    ``field_kind == "rich_text_html"``) — never a full document, and
    never fetched, read from disk or read over the network. This
    function performs no I/O of any kind.

    The returned root is a synthetic container — html5lib names it
    ``DOCUMENT_FRAGMENT`` under the ``body`` context; its children are
    the fragment's top-level nodes, in document order. Every comment,
    element and attribute this module's docstring names is present in
    the returned tree, untouched: this function only parses, it never
    decides what to keep.

    ``html5lib.parseFragment`` is untyped — its type stub declares no
    return annotation, so both the call and its result are ``Unknown``
    under ``pyright --strict`` (``reportUnknownMemberType`` on the call
    itself, ``reportUnknownVariableType`` on anything it is assigned to).
    The call is therefore made only inside this one ``cast`` expression,
    never assigned to a bare name first: the ``cast`` asserts the
    documented runtime return type (a plain
    ``xml.etree.ElementTree.Element`` when ``treebuilder="etree"``,
    verified against the installed 1.1 tree builder) and the
    ``pyright: ignore`` below is scoped to this single call, the one
    place in the codebase that assumes it. Every caller of this function
    sees only a concrete ``Element``.
    """
    return cast(
        Element,
        html5lib.parseFragment(  # pyright: ignore[reportUnknownMemberType]
            markup,
            container=_FRAGMENT_CONTAINER,
            treebuilder="etree",
            namespaceHTMLElements=False,
        ),
    )
