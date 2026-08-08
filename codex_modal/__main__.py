"""`python -m codex_modal` entry point, including the hidden watchdog subcommand."""

from __future__ import annotations

import sys
from pathlib import Path


def _configure_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _entry() -> int:
    _configure_stdio()
    if len(sys.argv) == 3 and sys.argv[1] == "__watchdog":
        from .lifecycle import watchdog_main

        return watchdog_main(Path(sys.argv[2]))
    from .cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_entry())
