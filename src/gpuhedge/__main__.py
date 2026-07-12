"""Enable ``python -m gpuhedge`` as an alias for the ``gpuhedge`` console script."""

from __future__ import annotations

from gpuhedge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
