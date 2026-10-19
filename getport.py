"""Print a free TCP port on the loopback interface.

Used by ``lyrics2pptx.vbs`` (ADR-017): the launcher asks this helper for an
ephemeral port, exports it as ``LYRICS2PPTX_BIND_PORT`` and only then starts the
web app. Keeping the port choice here (instead of hardcoding 8765) is what makes
"double click while 8765 is already taken" still work.

Only the standard library is used and the socket is closed before the port is
handed back, so the web app can bind it a moment later.
"""

from __future__ import annotations

import socket
import sys

BIND_HOST = "127.0.0.1"


def find_free_port(host: str = BIND_HOST) -> int:
    """Return an OS-assigned free port for ``host``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> int:
    print(find_free_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
