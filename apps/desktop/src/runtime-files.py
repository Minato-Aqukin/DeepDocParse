"""Fixed main-process bundle validator. No path or operation supplied by the renderer."""

import ctypes
import io
import os
import signal
import sys

from ddp_core.bundle import read_bundle


def main():
    parent = os.getppid()
    if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or parent == 1:
        return 1
    if os.getppid() != parent:
        return 1
    data = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        return 1
    try:
        read_bundle(io.BytesIO(data))
        sys.stdout.write("validated")
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
