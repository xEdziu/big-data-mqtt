"""Lokalny bufor (outbox) urządzenia brzegowego w SQLite.

Każdy pomiar najpierw trafia tutaj, a usuwany jest dopiero po potwierdzeniu
odbioru przez broker (PUBACK). Plik przetrwa restart procesu i całego kontenera.
"""

import sqlite3
from pathlib import Path


class Outbox:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " payload TEXT NOT NULL)"
        )
        self._db.commit()

    def append(self, payload: str) -> None:
        self._db.execute("INSERT INTO outbox (payload) VALUES (?)", (payload,))
        self._db.commit()

    def peek_oldest(self, limit: int) -> list[tuple[int, str]]:
        return self._db.execute(
            "SELECT id, payload FROM outbox ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
        self._db.commit()

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self) -> None:
        self._db.close()
