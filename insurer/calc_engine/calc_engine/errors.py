class EngineError(Exception):
    pass


class EngineInvariantError(EngineError):
    """An internal invariant (07 §2.3) failed. The API returns 500 with the trace."""

    def __init__(self, message: str, trace: list[object] | None = None) -> None:
        super().__init__(message)
        self.trace = trace or []


class RulesInvalidError(EngineError):
    pass


class TooManyLinesError(EngineError):
    pass
