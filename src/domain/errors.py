class DomainError(Exception):
    """Base for errors that represent a broken business rule rather than a bug."""


class InvalidOAuthState(DomainError):
    """The callback did not match an authorisation attempt we started, or it expired."""


class ProviderCursorExpired(DomainError):
    """The provider's incremental cursor is too old to use; fall back to a date search."""
