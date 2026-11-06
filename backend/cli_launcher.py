"""Console executable for native bundles (including macOS/Windows GUI builds)."""
import sys

if sys.platform == 'win32':
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')

from staffdeck_harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
