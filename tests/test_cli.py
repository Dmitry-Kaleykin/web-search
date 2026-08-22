from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_research.cli import _browser_runtime_present


class DoctorBrowserRuntimeTests(unittest.TestCase):
    def test_detects_browser_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "chromium-1" / "chrome-mac" / "Chromium"
            binary.parent.mkdir(parents=True)
            binary.touch()

            self.assertTrue(_browser_runtime_present(Path(directory)))

    def test_rejects_empty_browser_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(_browser_runtime_present(Path(directory)))


if __name__ == "__main__":
    unittest.main()
