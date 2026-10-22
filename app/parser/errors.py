class ParseError(Exception):
    pass


class BrowserError(ParseError):
    """Browser startup, navigation or process failure eligible for a fallback."""


class EbayAccessError(ParseError):
    def __init__(self, message: str, retry_at: float, *, status_code: int | None = None):
        super().__init__(message)
        self.retry_at = retry_at
        self.status_code = status_code
