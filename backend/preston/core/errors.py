"""Application-level error foundation.

Errors are framework-free so they can be raised from any layer. The HTTP
translation lives in the API layer and follows RFC 9457 (Problem Details).
"""


class PrestonError(Exception):
    """Base class for expected application errors.

    Attributes:
        title: Short, human-readable summary of the problem type.
        status_code: HTTP status the API layer should use.
    """

    title: str = "Internal Server Error"
    status_code: int = 500

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
