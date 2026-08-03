import hashlib
import json
import re
import unicodedata

ISSUE_EMBEDDING_TEXT_VERSION = "issue-title-body-v2"
EMPTY_BODY_MARKER = "[empty]"
EMPTY_LABELS_MARKER = "[none]"


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


def normalize_labels(labels: list[str] | None) -> list[str]:
    values = {normalize_text(label) for label in labels or [] if normalize_text(label)}
    return sorted(values, key=lambda item: (item.casefold(), item))


def issue_embedding_text(
    title: str, body: str | None, labels: list[str] | None = None
) -> str:
    normalized_body = normalize_text(body) or EMPTY_BODY_MARKER
    # Retrieval input intentionally excludes labels. In evaluation, labels may be
    # assigned after duplicate resolution and would leak maintainer decisions.
    return f"Title: {normalize_text(title)}\nBody:\n{normalized_body}"


def issue_content_hash(title: str, body: str | None, labels: list[str] | None) -> str:
    canonical = json.dumps(
        {
            "title": normalize_text(title),
            "body": normalize_text(body),
            # Labels are stored for display but are not retrieval input. Excluding
            # them also prevents label-only edits from regenerating embeddings.
            "text_version": ISSUE_EMBEDDING_TEXT_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
