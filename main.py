#!/usr/bin/env python3
"""Entry point: `python main.py <subcommand>`.

Adds ``src/`` to sys.path so the CLI works from a fresh clone without setting
PYTHONPATH (the installed console script ``conso`` gets the same module via the
package install).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from conso.app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
