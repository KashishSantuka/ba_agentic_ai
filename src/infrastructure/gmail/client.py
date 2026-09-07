"""Thin wrapper over the Gmail API. Knows Google's shapes, nothing about our domain."""

import base64

from google.auth.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.domain.errors import ProviderCursorExpired


class GmailClient:
    def __init__(self, credentials: Credentials):
        self._service = build("gmail", "v1", credentials=credentials)

    def get_profile_email(self) -> str:
        return self._service.users().getProfile(userId="me").execute()["emailAddress"]

    def list_message_ids_since(self, history_id: str) -> tuple[list[str], str]:
        """Incremental fetch. Raises ProviderCursorExpired when Gmail no longer knows
        the history id (it keeps roughly a week)."""
        message_ids: list[str] = []
        latest_history_id = history_id
        page_token = None

        while True:
            try:
                response = (
                    self._service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=history_id,
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise ProviderCursorExpired from exc
                raise

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_ids.append(added["message"]["id"])

            latest_history_id = response.get("historyId", latest_history_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids, latest_history_id

    def list_message_ids_by_query(self, query: str) -> tuple[list[str], str]:
        """Date-bounded fetch, used on a first sync or when the cursor has expired."""
        message_ids: list[str] = []
        page_token = None

        while True:
            response = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token)
                .execute()
            )
            message_ids.extend(m["id"] for m in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        profile = self._service.users().getProfile(userId="me").execute()
        return message_ids, str(profile["historyId"])

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = (
            self._service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(response["data"].encode("utf-8"))

    def get_message(self, message_id: str) -> dict:
        return (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
