class DomainError(Exception):
    """Base for errors that represent a broken business rule rather than a bug."""


class InvalidOAuthState(DomainError):
    """The callback did not match an authorisation attempt we started, or it expired."""


class ProviderCursorExpired(DomainError):
    """The provider's incremental cursor is too old to use; fall back to a date search."""


class ClassificationFailed(DomainError):
    """No usable answer came back from the classifier — the call failed, the response was
    not the agreed shape, or the label was not one we recognise. Carries the reason so it
    can be written to the audit log and read months later."""
