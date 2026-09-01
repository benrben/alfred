#!/usr/bin/env python3
"""Entry-point shim: `voicebridge/` (the package) shadows this file for
`import voicebridge`, so this only matters for direct invocation
(`python3 voicebridge.py <args>`, what every front end and the docs use)."""

from voicebridge import main

if __name__ == "__main__":
    raise SystemExit(main())
