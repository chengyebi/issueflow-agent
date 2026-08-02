from collections.abc import Iterator

import psycopg
from psycopg import Connection

from app.core.config import get_settings


def connect(*, row_factory=None) -> Connection:
    kwargs = {}
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    return psycopg.connect(get_settings().database_url, **kwargs)


def connection() -> Iterator[Connection]:
    with connect() as conn:
        yield conn

