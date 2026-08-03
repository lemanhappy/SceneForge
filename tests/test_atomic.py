"""Tests for atomic, lock-guarded config writes."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from utils.atomic import atomic_write_text, file_lock


class TestAtomicWrite(unittest.TestCase):
    def test_writes_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "c.yaml")
            atomic_write_text(p, "a: 1\n")
            self.assertEqual(Path(p).read_text(encoding="utf-8"), "a: 1\n")

    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as d:
            atomic_write_text(os.path.join(d, "c.yaml"), "x: 1\n")
            leftovers = [f for f in os.listdir(d) if f.startswith(".tmp_")]
            self.assertEqual(leftovers, [])

    def test_overwrite_is_complete_not_partial(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            atomic_write_text(p, "old: data\n")
            atomic_write_text(p, "new: value\nmore: stuff\n")
            self.assertEqual(yaml.safe_load(Path(p).read_text(encoding="utf-8")), {"new": "value", "more": "stuff"})

    def test_concurrent_writes_never_corrupt(self):
        # Many threads hammer the same file; every read must parse as valid YAML
        # (atomic replace guarantees readers see one whole version or another).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            atomic_write_text(p, "n: 0\n")
            errors = []

            def writer(i):
                try:
                    for _ in range(20):
                        atomic_write_text(p, yaml.safe_dump({"n": i, "pad": "x" * 500}))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            def read_once():
                # On Windows an open() racing os.replace can transiently raise
                # PermissionError; that's retryable, not corruption. Retry it.
                for _ in range(50):
                    try:
                        return Path(p).read_text(encoding="utf-8")
                    except PermissionError:
                        import time as _t; _t.sleep(0.005)
                return Path(p).read_text(encoding="utf-8")

            def reader():
                try:
                    for _ in range(50):
                        loaded = yaml.safe_load(read_once())
                        # the real guarantee: never a torn/partial file
                        self.assertIsInstance(loaded, dict)
                        self.assertIn("n", loaded)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + \
                      [threading.Thread(target=reader) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])

    def test_file_lock_serializes_read_modify_write(self):
        # Concurrent increments under file_lock must not lose updates.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            atomic_write_text(p, yaml.safe_dump({"n": 0}))

            def inc():
                for _ in range(50):
                    with file_lock(p):
                        data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
                        data["n"] += 1
                        atomic_write_text(p, yaml.safe_dump(data))

            threads = [threading.Thread(target=inc) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(yaml.safe_load(Path(p).read_text(encoding="utf-8"))["n"], 200)


if __name__ == "__main__":
    unittest.main()
