"""Local-disk implementation of the AttachmentStore port."""

import re
import unicodedata
from pathlib import Path

from src.domain.entities import Attachment
from src.domain.ports import AttachmentStore

UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(filename: str) -> str:
    """A sender chooses the filename, so it is never trusted as a path.

    Everything outside a conservative character set is replaced, which flattens `../`
    traversal and separators alike; the result is a single path segment.
    """
    name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    name = UNSAFE.sub("_", name).lstrip(".")
    return (name or "attachment")[:120]


class FileAttachmentStore(AttachmentStore):
    """Writes bytes under <root>/<connection_id>/<source_id>/<remote_id>-<filename>.

    Keyed by remote_id as well as filename because one message may legitimately carry
    two files of the same name.
    """

    def __init__(self, root: Path):
        self._root = root

    def save(
        self, connection_id: int, source_id: str, attachment: Attachment, content: bytes
    ) -> str:
        directory = self._root / str(connection_id) / _safe_name(source_id)
        directory.mkdir(parents=True, exist_ok=True)

        path = directory / f"{_safe_name(attachment.remote_id)[:40]}-{_safe_name(attachment.filename)}"
        path.write_bytes(content)
        return str(path)
