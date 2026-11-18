class ContextValidationError(ValueError):
    """Raised when context data violates the shared contract."""


class FormalRunPreparationRequired(ContextValidationError):
    """A selected declaration needs a committed run before compilation."""

    def __init__(self, declaration_id: str):
        self.declaration_id = declaration_id
        super().__init__(
            f"formal declaration {declaration_id!r} requires a committed "
            "FormalRun, root FormalState, and pinned Environment before "
            "context compilation"
        )
