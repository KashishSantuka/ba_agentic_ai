"""What the adapter does with what the model returns.

The schema sent to Gemini asks for the right shape but does not guarantee it, so these
cover the cases where the response is wrong and must not reach the audit log.
"""

from datetime import datetime, timezone

import pytest

from src.domain.entities import ClassificationLabel, RawDocument
from src.domain.errors import ClassificationFailed
from src.infrastructure.gemini.classifier import GeminiDocumentClassifier


class FakeInteraction:
    def __init__(self, output_text):
        self.output_text = output_text


class FakeInteractions:
    def __init__(self, output_text=None, error: Exception | None = None):
        self._output_text = output_text
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeInteraction(self._output_text)


class FakeClient:
    def __init__(self, output_text=None, error: Exception | None = None):
        self.interactions = FakeInteractions(output_text, error)


def make_classifier(output_text=None, error: Exception | None = None, body_char_limit=1000):
    client = FakeClient(output_text, error)
    return GeminiDocumentClassifier(
        client=client, model="gemini-test-001", body_char_limit=body_char_limit
    ), client


def make_document(body: str = "Our March invoice is short by $4,000.") -> RawDocument:
    return RawDocument(
        source="gmail",
        source_id="msg-1",
        subject="Invoice query",
        sender="Acme Ltd",
        sender_email="ap@acme.example",
        sent_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        body_text=body,
    )


def test_a_well_formed_answer_is_returned():
    classifier, _ = make_classifier(
        '{"classification": "relevant", "confidence": 0.91, "reason": "Invoice mismatch"}'
    )

    result = classifier.classify(make_document())

    assert result.label == ClassificationLabel.RELEVANT
    assert result.confidence == 0.91
    assert result.reason == "Invoice mismatch"


def test_the_prompt_receives_sender_subject_and_body():
    classifier, client = make_classifier(
        '{"classification": "junk", "confidence": 0.99, "reason": "Spam"}'
    )

    classifier.classify(make_document())

    sent = client.interactions.calls[0]["input"]
    assert "ap@acme.example" in sent
    assert "Invoice query" in sent
    assert "short by $4,000" in sent


def test_the_body_is_truncated_to_the_configured_limit():
    classifier, client = make_classifier(
        '{"classification": "junk", "confidence": 0.99, "reason": "Spam"}', body_char_limit=20
    )

    classifier.classify(make_document(body="x" * 500))

    assert "x" * 20 in client.interactions.calls[0]["input"]
    assert "x" * 21 not in client.interactions.calls[0]["input"]


def test_temperature_is_zero_so_the_same_email_does_not_drift_between_runs():
    classifier, client = make_classifier(
        '{"classification": "junk", "confidence": 0.99, "reason": "Spam"}'
    )

    classifier.classify(make_document())

    assert client.interactions.calls[0]["generation_config"]["temperature"] == 0.0


@pytest.mark.parametrize(
    "output_text",
    [
        pytest.param("not json at all", id="not_json"),
        pytest.param("", id="empty"),
        pytest.param('["relevant"]', id="not_an_object"),
        # A label nobody defined would otherwise be counted in every report built on the
        # audit log afterwards.
        pytest.param('{"classification": "maybe", "confidence": 0.5, "reason": "x"}', id="bad_label"),
        pytest.param('{"classification": "relevant", "confidence": 1.7, "reason": "x"}', id="confidence_high"),
        pytest.param('{"classification": "relevant", "confidence": -0.2, "reason": "x"}', id="confidence_low"),
        pytest.param('{"classification": "relevant", "confidence": "high", "reason": "x"}', id="confidence_not_a_number"),
        pytest.param('{"confidence": 0.9, "reason": "x"}', id="no_label"),
    ],
)
def test_an_unusable_response_is_a_failed_attempt_rather_than_a_guess(output_text):
    classifier, _ = make_classifier(output_text)

    with pytest.raises(ClassificationFailed):
        classifier.classify(make_document())


def test_a_transport_error_becomes_a_failed_attempt():
    # The use case retries a ClassificationFailed; anything else would escape and take
    # the rest of the batch down with it.
    classifier, _ = make_classifier(error=TimeoutError("upstream timed out"))

    with pytest.raises(ClassificationFailed, match="TimeoutError"):
        classifier.classify(make_document())
