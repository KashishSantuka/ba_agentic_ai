"""Gmail implementation of the OAuthProvider port."""

from datetime import timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from src.domain.entities import OAuthTokens
from src.domain.ports import OAuthProvider
from src.infrastructure.config import Settings
from src.infrastructure.gmail.client import GmailClient

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def credentials_from_tokens(settings: Settings, tokens: OAuthTokens) -> Credentials:
    """google-auth expects a naive UTC expiry; our domain uses timezone-aware datetimes."""
    expiry = tokens.expires_at
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)

    return Credentials(
        token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
        expiry=expiry,
    )


def tokens_from_credentials(credentials: Credentials) -> OAuthTokens:
    expires_at = credentials.expiry
    if expires_at is not None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return OAuthTokens(
        access_token=credentials.token,
        refresh_token=credentials.refresh_token,
        expires_at=expires_at,
        scope=" ".join(credentials.scopes or SCOPES),
    )


class GmailOAuthProvider(OAuthProvider):
    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def name(self) -> str:
        return "gmail"

    def _flow(self) -> Flow:
        if not self._settings.google_client_id or not self._settings.google_client_secret:
            raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")

        client_config = {
            "web": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": [self._settings.google_redirect_uri],
            }
        }
        # PKCE off: the verifier is generated in /connect and would have to survive into
        # /callback, but each request builds its own Flow. A web client authenticates the
        # token exchange with its client_secret, so the verifier is not required.
        return Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=self._settings.google_redirect_uri,
            autogenerate_code_verifier=False,
        )

    def authorization_url(self, state: str, login_hint: str | None = None) -> str:
        url, _ = self._flow().authorization_url(
            access_type="offline",        # request a refresh token so syncing runs unattended
            include_granted_scopes="true",
            prompt="consent",             # force a refresh token even when re-approving
            state=state,
            login_hint=login_hint,
        )
        return url

    def exchange_code(self, code: str) -> tuple[OAuthTokens, str]:
        flow = self._flow()
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Gmail tells us which mailbox was granted; we never name it in the request.
        mailbox_email = GmailClient(credentials).get_profile_email()

        return tokens_from_credentials(credentials), mailbox_email

    def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        credentials = credentials_from_tokens(self._settings, tokens)
        credentials.refresh(Request())
        return tokens_from_credentials(credentials)
