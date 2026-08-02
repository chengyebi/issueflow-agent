import re

_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(authorization|token|api[_-]?key|signature|secret)(\s*[:=]\s*)\S+"),
    re.compile(r"sha256=[0-9a-fA-F]{32,}"),
)


def sanitize_error_message(exc: BaseException | str, limit: int = 1000) -> str:
    value = str(exc).replace("\n", " ").replace("\r", " ")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[:limit]
