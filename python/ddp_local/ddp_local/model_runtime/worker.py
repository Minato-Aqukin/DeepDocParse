"""Set parent ownership before exec; the CPU binary never survives its runtime owner."""

import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    if sys.platform != "linux":
        raise SystemExit("this reviewed CPU runtime is Linux only")
    if ctypes.CDLL(None).prctl(1, signal.SIGTERM) != 0 or os.getppid() != parent:
        raise SystemExit("runtime owner disappeared before model launch")
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
