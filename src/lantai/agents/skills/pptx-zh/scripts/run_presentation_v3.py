#!/usr/bin/env python3
"""Start Lantai's independently implemented Presentation Runtime v3."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from lantai.presentation import cli as presentation_cli


def run(arguments: Sequence[str] | None = None) -> int:
    """Forward a bounded argument list to the repository-owned v3 CLI."""

    forwarded = list(arguments) if arguments is not None else sys.argv[1:]
    return presentation_cli.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(run())
