from __future__ import annotations

import unittest
from pathlib import Path

from codex_modal.paths import PROJECT_ROOT


class LayoutTests(unittest.TestCase):
    def test_launchers_are_thin_and_no_scripts_are_required(self) -> None:
        powershell = (PROJECT_ROOT / "codex-modal.ps1").read_text(encoding="utf-8")
        shell = (PROJECT_ROOT / "codex-modal.sh").read_text(encoding="utf-8")
        self.assertLess(len(powershell), 4_000)
        self.assertLess(len(shell), 2_500)
        self.assertIn("-m codex_modal", powershell)
        self.assertIn("-m codex_modal", shell)
        self.assertNotIn("endpoint create", powershell)
        self.assertNotIn("endpoint create", shell)
        scripts = PROJECT_ROOT / "scripts"
        self.assertFalse(scripts.exists() and any(scripts.iterdir()))
        self.assertFalse((PROJECT_ROOT / "setup.ps1").exists())


if __name__ == "__main__":
    unittest.main()
