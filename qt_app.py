"""Compatibility launcher for the canonical Qt application in :mod:`app`."""

from app import *
from app import main


if __name__ == "__main__":
    raise SystemExit(main())
