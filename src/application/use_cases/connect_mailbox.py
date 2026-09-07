"""Connecting a mailbox: start the authorisation, then complete it."""

import secrets
from dataclasses import dataclass

from src.domain.entities import EmailConnection
from src.domain.errors import InvalidOAuthState
from src.domain.ports import ConnectionRepository, OAuthProvider, OAuthStateStore


@dataclass
class StartMailboxConnection:
    provider: OAuthProvider
    states: OAuthStateStore

    def execute(self, login_hint: str | None = None) -> str:
        self.states.purge_expired()

        state = secrets.token_urlsafe(32)
        self.states.issue(state)

        return self.provider.authorization_url(state=state, login_hint=login_hint)


@dataclass
class CompleteMailboxConnection:
    provider: OAuthProvider
    states: OAuthStateStore
    connections: ConnectionRepository

    def execute(self, code: str, state: str) -> EmailConnection:
        if not self.states.consume(state):
            raise InvalidOAuthState(
                "Unrecognised or expired state — start again from the connect endpoint"
            )

        tokens, mailbox_email = self.provider.exchange_code(code)

        return self.connections.upsert(
            EmailConnection(
                provider=self.provider.name,
                mailbox_email=mailbox_email,
                tokens=tokens,
            )
        )
