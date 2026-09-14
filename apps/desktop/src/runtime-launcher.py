"""Trusted host bootstrap; Linux children stop if this Electron parent disappears."""

import ctypes
import json
import os
import platform
import signal
import sys
import sysconfig
from pathlib import Path


def main():
    if sys.platform != "linux":
        raise SystemExit("desktop runtime platform not validated")
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or parent == 1 or os.getppid() != parent:
        raise SystemExit("desktop runtime parent unavailable")
    # Directory packages use the declared Arch Python ABI. Never import a mismatched native wheel.
    configuration = Path(__file__).resolve().parents[2] / "runtime" / "runtime-info.json"
    if configuration.exists():
        abi = json.loads(configuration.read_text())
        if (
            abi["python_major_minor"] != list(sys.version_info[:2])
            or abi["cache_tag"] != sys.implementation.cache_tag
            or abi["soabi"] != sysconfig.get_config_var("SOABI")
            or abi["machine"] != platform.machine()
        ):
            raise SystemExit(
                "desktop runtime requires its packaged Python ABI; rebuild the package"
            )
    from ddp_local.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
