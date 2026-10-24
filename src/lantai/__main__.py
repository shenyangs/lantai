# -*- coding: utf-8 -*-
"""Allow running Lantai via ``python -m lantai``."""
import sys

if sys.argv[1:] == ["--desktop-process-probe"]:
    from .security.desktop_process_probe import main

    raise SystemExit(main())

from .cli.main import cli

if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter
