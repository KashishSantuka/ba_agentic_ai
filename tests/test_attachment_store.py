"""The filename comes from the sender, so the store is tested as untrusted input."""

from pathlib import Path

from src.domain.entities import Attachment
from src.infrastructure.storage.files import FileAttachmentStore


def _attachment(filename: str, remote_id: str = "att_1") -> Attachment:
    return Attachment(
        filename=filename, mime_type="application/pdf", size_bytes=4, remote_id=remote_id
    )


def test_bytes_land_under_the_connection_and_message(tmp_path):
    store = FileAttachmentStore(tmp_path)

    location = Path(store.save(1, "msg_001", _attachment("spec.pdf"), b"data"))

    assert location.parent == tmp_path / "1" / "msg_001"
    assert location.name.endswith("spec.pdf")
    assert location.read_bytes() == b"data"


def test_a_traversing_filename_cannot_escape_the_root(tmp_path):
    store = FileAttachmentStore(tmp_path)

    location = Path(store.save(1, "msg_001", _attachment("../../../../etc/passwd"), b"data"))

    # Flattened to a single segment, so the file stays inside the store.
    assert tmp_path in location.parents
    assert location.parent == tmp_path / "1" / "msg_001"
    assert "/" not in location.name.split("-", 1)[1]


def test_two_files_of_the_same_name_do_not_overwrite_each_other(tmp_path):
    store = FileAttachmentStore(tmp_path)

    first = Path(store.save(1, "msg_001", _attachment("report.pdf", "att_1"), b"one"))
    second = Path(store.save(1, "msg_001", _attachment("report.pdf", "att_2"), b"two"))

    # Keyed by the provider's id, not the filename, which a sender controls.
    assert first != second
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
