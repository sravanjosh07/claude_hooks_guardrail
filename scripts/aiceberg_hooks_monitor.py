#!/usr/bin/env python3
"""Compatibility wrapper.

The implementation now lives under scripts/aiceberg_hooks/.
This file remains as the stable entrypoint referenced by hooks.json.
"""

from __future__ import annotations

from aiceberg_hooks import main


if __name__ == "__main__":
    raise SystemExit(main())
