import sqlite3
from contextlib import closing

import pytest

from scripts.database_maintenance import _restore


def _create_database(path, value):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))
        connection.commit()


def test_restore_requires_force_and_distinct_source(tmp_path):
    source = tmp_path / "backup.db"
    _create_database(source, "backup")
    with pytest.raises(SystemExit, match="--force"):
        _restore(source, tmp_path / "active.db", force=False)
    with pytest.raises(SystemExit, match="must differ"):
        _restore(source, source, force=True)


def test_restore_replaces_database_and_clears_stale_wal_files(tmp_path):
    source = tmp_path / "backup.db"
    destination = tmp_path / "active.db"
    _create_database(source, "backup")
    _create_database(destination, "old")
    wal = destination.with_name(destination.name + "-wal")
    shm = destination.with_name(destination.name + "-shm")
    wal.write_bytes(b"stale")
    shm.write_bytes(b"stale")

    _restore(source, destination, force=True)

    assert not wal.exists()
    assert not shm.exists()
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "backup"
