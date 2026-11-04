"""Shared exception type of the quality package (re-exported from spec.py)."""


class QualityError(ValueError):
    """User-facing error in an axc_* attribute, a stimulus file or a plugin. The message is final text."""
