from __future__ import annotations

from core.store.base import AbstractStore

_store: AbstractStore | None = None


def get_store() -> AbstractStore:
    global _store
    if _store is None:
        from core.config import settings
        if settings.store_backend == "postgres":
            from core.store.postgres_store import PostgresStore
            _store = PostgresStore()
        else:
            from core.store.sqlite_store import SqliteStore
            _store = SqliteStore()
    return _store


def init_db() -> None:
    get_store().init_db()


def insert(result) -> None:
    get_store().insert(result)


def latest(metric_name: str):
    return get_store().latest(metric_name)


def latest_all():
    return get_store().latest_all()


def recent(hours: int = 24):
    return get_store().recent(hours)
