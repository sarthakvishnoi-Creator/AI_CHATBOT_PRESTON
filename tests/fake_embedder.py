"""A deterministic stand-in for an embedding provider.

Shared by every test that needs vectors without a network: it satisfies
:class:`preston.embedding.Embedder`, so code written against that interface
runs unchanged against it.
"""

import hashlib
from collections.abc import Sequence


class FakeEmbedder:
    """Maps each text to its own fixed vector of ``dimensions`` floats.

    The vector depends only on the text: the same text always gives the same
    vector and different texts give different ones, so a test can tell
    whether vectors came back in input order. Values lie in ``[0, 1)``.
    """

    def __init__(self, dimensions: int = 8) -> None:
        self.dimensions = dimensions
        #: Every batch received, so tests can count and inspect calls.
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector_for(text) for text in texts]

    def vector_for(self, text: str) -> list[float]:
        values: list[float] = []
        block = 0
        while len(values) < self.dimensions:
            digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
            values.extend(byte / 256 for byte in digest)
            block += 1
        return values[: self.dimensions]
