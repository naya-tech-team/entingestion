from __future__ import annotations

from pathlib import Path

import pytest

from ingest.vectorstore.migrations import MigrationError, load

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"


def test_repository_migrations_load_in_order() -> None:
    migrations = load(MIGRATIONS)
    assert migrations[0].version == "001"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert "halfvec(1024)" in migrations[0].sql


def test_bad_names_and_duplicates_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "1-bad.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="unexpected"):
        load(tmp_path)

    (tmp_path / "1-bad.sql").unlink()
    (tmp_path / "001_dup.sql").write_text("SELECT 2;")
    with pytest.raises(MigrationError, match="duplicate"):
        load(tmp_path)
