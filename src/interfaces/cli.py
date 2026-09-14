"""CLI for local operations. Connecting a mailbox lives in the API instead, because
OAuth needs a real browser redirect to a stable URL."""

import argparse
import logging

from src import container
from src.infrastructure.config import settings
from src.infrastructure.persistence.session import SessionLocal, init_db

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")


def cmd_init_db(_args: argparse.Namespace) -> None:
    init_db()
    print("Database initialized.")


def cmd_connections(_args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        connections = container.build_connection_repository(session).list_all()

    if not connections:
        print("No mailboxes connected yet.")
        print("Start the API and open http://localhost:8000/auth/gmail/connect")
        return

    for c in connections:
        print(f"[{c.id}] {c.mailbox_email}  connected_at={c.connected_at}")


def cmd_sync(_args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        results = container.build_sync_all(session).execute()

    if not results:
        print("No mailboxes connected yet — nothing to sync.")
        return

    for result in results:
        print(
            f"{result.mailbox_email}: {result.new_messages} new, "
            f"{result.new_attachments} attachment(s)"
        )
        for document in result.documents:
            print(f"   - {document.subject}  <{document.sender_email}>")
            for attachment in document.attachments:
                print(f"       * {attachment.filename}  ({attachment.size_bytes} bytes)")


def cmd_classify(_args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        result = container.build_classify_pending(session).execute()

    print(
        f"{result.classified} classified, {result.failed} failed, "
        f"{result.sent_to_review} sent to review, {result.reclaimed} reclaimed"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ba-agentic-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create the database tables").set_defaults(
        func=cmd_init_db
    )
    subparsers.add_parser("connections", help="List connected mailboxes").set_defaults(
        func=cmd_connections
    )
    subparsers.add_parser("sync", help="Fetch new mail for every connected mailbox").set_defaults(
        func=cmd_sync
    )
    subparsers.add_parser(
        "classify", help="Classify one batch of pending documents"
    ).set_defaults(func=cmd_classify)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
