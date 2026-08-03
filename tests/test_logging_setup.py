"""Tests for central logging configuration."""

import logging
import os
import tempfile
import unittest


class TestLoggingSetup(unittest.TestCase):
    def setUp(self):
        # reset the module's one-shot guard + root handlers for an isolated test
        import utils.logging_setup as ls
        ls._configured = False
        self._saved = logging.getLogger().handlers[:]

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self._saved
        import utils.logging_setup as ls
        ls._configured = False

    def test_adds_stream_handler_and_is_idempotent(self):
        from utils.logging_setup import configure_logging
        before = len(logging.getLogger().handlers)
        configure_logging(level="INFO")
        after_first = len(logging.getLogger().handlers)
        configure_logging(level="INFO")  # idempotent
        after_second = len(logging.getLogger().handlers)
        self.assertEqual(after_first, before + 1)
        self.assertEqual(after_second, after_first)

    def test_writes_to_logfile(self):
        from utils.logging_setup import configure_logging
        with tempfile.TemporaryDirectory() as d:
            logfile = os.path.join(d, "sceneforge.log")
            configure_logging(level="INFO", logfile=logfile)
            logging.getLogger("test.x").warning("hello-log")
            root = logging.getLogger()
            for h in root.handlers:
                h.flush()
            self.assertTrue(os.path.exists(logfile))
            content = open(logfile, encoding="utf-8").read()
            # close + drop file handlers so Windows can remove the temp dir
            for h in list(root.handlers):
                if isinstance(h, logging.FileHandler):
                    h.close()
                    root.removeHandler(h)
            self.assertIn("hello-log", content)


if __name__ == "__main__":
    unittest.main()
