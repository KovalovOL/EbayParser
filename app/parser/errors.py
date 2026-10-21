class ParseError(Exception):
    pass


class EbayAccessError(ParseError):
    def __init__(self, message: str, retry_at: float):
        super().__init__(message)
        self.retry_at = retry_at
