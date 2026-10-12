"""Plain-SQL migrations (db/migrations/NNN_name.sql), applied in order and recorded once."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

_FILENAME = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")
# Serializes concurrent migrate invocations (arbitrary constant, unique to this purpose).
LOCK_KEY = 0x1A9E57


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


class MigrationError(RuntimeError):
    pass


def load(directory: Path) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"unexpected migration file name: {path.name}")
        migrations.append(Migration(match["version"], match["name"], path.read_text()))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {directory}")
    return migrations


def apply(conn: psycopg.Connection[tuple[object, ...]], directory: Path) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the versions applied.

    An applied migration whose file changed is an error: migrations are immutable once run.
    """
    conn.autocommit = True
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        applied = {str(version): str(checksum) for version, checksum in rows}
        done: list[str] = []
        for migration in load(directory):
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    label = f"{migration.version}_{migration.name}"
                    raise MigrationError(f"migration {label} changed after it was applied")
                continue
            with conn.transaction():
                conn.execute(migration.sql.encode())
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            done.append(migration.version)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
