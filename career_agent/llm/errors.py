class LLMError(RuntimeError):
    """A model call failed. The message is written for the user."""


class LLMConfigError(LLMError):
    """The model isn't set up (missing key, local-only violation, model not installed...)."""


class LLMRefusal(LLMError):
    """The model declined the request."""
