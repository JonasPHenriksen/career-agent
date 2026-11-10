class LLMError(RuntimeError):
    """A model call failed. The message is written for the user."""


class LLMConfigError(LLMError):
    """The model isn't set up (missing key, local-only violation, model not installed...)."""


class LLMRefusal(LLMError):
    """The model declined the request."""


class LLMTransient(LLMError):
    """A passing problem (a server restarting, a rate or usage limit): worth trying again later.
    `retry_after` is a suggested wait in seconds, if the provider said."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
