"""Gmail implementation of the MailboxReader port."""

import logging
from typing import Callable, Iterator

from src.domain.entities import RawDocument, SyncBookmark
from src.domain.errors import ProviderCursorExpired
from src.domain.ports import MailboxReader
from src.infrastructure.gmail.client import GmailClient
from src.infrastructure.gmail.parser import parse_gmail_message

logger = logging.getLogger(__name__)


class GmailMailboxReader(MailboxReader):
    def __init__(self, client: GmailClient):
        self._client = client

    def fetch_since(
        self,
        bookmark: SyncBookmark,
        default_lookback_days: int,
        should_skip: Callable[[str], bool] | None = None,
    ) -> tuple[Iterator[RawDocument], str]:
        message_ids, next_cursor = self._list_message_ids(bookmark, default_lookback_days)
        return self._documents(message_ids, should_skip), next_cursor

    def fetch_attachment(self, source_id: str, remote_id: str) -> bytes:
        return self._client.get_attachment(source_id, remote_id)

    def _list_message_ids(
        self, bookmark: SyncBookmark, default_lookback_days: int
    ) -> tuple[list[str], str]:
        if bookmark.last_history_id:
            try:
                return self._client.list_message_ids_since(bookmark.last_history_id)
            except ProviderCursorExpired:
                logger.warning(
                    "Gmail history id %s expired; falling back to a date-bounded search",
                    bookmark.last_history_id,
                )

        days = bookmark.lookback_days(default_lookback_days)
        return self._client.list_message_ids_by_query(f"newer_than:{days}d")

    def _documents(
        self, message_ids: list[str], should_skip: Callable[[str], bool] | None
    ) -> Iterator[RawDocument]:
        for message_id in message_ids:
            # Checked before get_message: skipping here avoids downloading bodies we
            # already handled, which is most of them on any re-run.
            if should_skip is not None and should_skip(message_id):
                continue
            yield parse_gmail_message(self._client.get_message(message_id))
