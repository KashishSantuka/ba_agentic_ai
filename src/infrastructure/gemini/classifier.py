"""The Gemini-backed scope classifier.

The only module that knows Gemini exists. Everything above it depends on the
DocumentClassifier port, so a different model — or a second opinion from another
provider — is a new class here and nothing else.
"""

import json
import logging

from src.domain.entities import Classification, ClassificationLabel, RawDocument
from src.domain.errors import ClassificationFailed
from src.domain.ports import DocumentClassifier
from src.infrastructure.gemini.prompt import PROMPT_VERSION, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Asking the model to honour a schema is cheaper than repairing free text afterwards, but
# it is not a guarantee: the response is still validated below before it is believed.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": sorted(ClassificationLabel.ALL)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["classification", "confidence", "reason"],
}

# A triage label is not a creative task: the same email should not land in different
# buckets on different days, so nothing is left to sampling.
TEMPERATURE = 0.0

# The answer is three short fields. A cap keeps a model that starts narrating from
# running up a bill on an email that was never going to classify cleanly.
MAX_OUTPUT_TOKENS = 256

# Kept short so the log stays readable and no email content is copied into it wholesale.
MAX_REASON_CHARS = 300


class GeminiDocumentClassifier(DocumentClassifier):
    def __init__(self, client, model: str, body_char_limit: int):
        self._client = client
        self._model = model
        self._body_char_limit = body_char_limit

    @property
    def model_version(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    def classify(self, document: RawDocument) -> Classification:
        try:
            interaction = self._client.interactions.create(
                model=self._model,
                system_instruction=SYSTEM_PROMPT,
                input=self._render(document),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": RESPONSE_SCHEMA,
                },
                generation_config={
                    "temperature": TEMPERATURE,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                },
            )
        except Exception as exc:
            # Every transport and quota problem arrives here. It is a failed attempt, not
            # a crash: the document goes back in the queue and the reason is recorded.
            raise ClassificationFailed(f"{type(exc).__name__}: {exc}") from exc

        return self._parse(interaction.output_text)

    def _render(self, document: RawDocument) -> str:
        """The three fields the prompt promises, and nothing else.

        Attachments are deliberately not described. This stage decides only whether the
        message is worth reading further, and a filename is a poor signal for that.
        """
        body = (document.body_text or "").strip()
        if len(body) > self._body_char_limit:
            body = body[: self._body_char_limit]

        return (
            f"Sender: {document.sender} <{document.sender_email}>\n"
            f"Subject: {document.subject}\n"
            f"Body:\n{body}"
        )

    def _parse(self, raw: str | None) -> Classification:
        if not raw or not raw.strip():
            raise ClassificationFailed("empty response")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClassificationFailed(f"response was not JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ClassificationFailed("response was not a JSON object")

        label = payload.get("classification")
        if label not in ClassificationLabel.ALL:
            # An invented label would otherwise flow into the audit log and be counted in
            # every report built on it afterwards.
            raise ClassificationFailed(f"unknown classification {label!r}")

        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ClassificationFailed(f"confidence was not a number: {confidence!r}")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ClassificationFailed(f"confidence out of range: {confidence!r}")

        reason = payload.get("reason")
        if not isinstance(reason, str):
            reason = ""

        return Classification(
            label=label,
            confidence=float(confidence),
            reason=reason.strip()[:MAX_REASON_CHARS],
        )
