"""Test-only socket audit in the same process as the real desktop runtime.

The native launcher uses -S, so sitecustomize is intentionally not loaded.
The test's supported spawnProcess seam runs this bootstrap before the unchanged
trusted launcher; no runtime network operation is replaced or mocked.
"""

import ipaddress
import json
import os
import runpy
import socket
import sys


def loopback(host):
    if host in (None, "", "localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(
            getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


audit_file = os.environ["DDP_TEST_SOCKET_AUDIT_FILE"]


def audit(event, args):
    if event == "socket.getaddrinfo":
        host, port = args[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        local = loopback(host)
    elif event == "socket.connect":
        connection, address = args
        if connection.family == socket.AF_UNIX:
            host, port, local = "unix", None, True
        else:
            host, port = address[:2]
            local = loopback(host)
    else:
        return
    row = json.dumps({"event": event, "host": host, "port": port,
                      "loopback_or_unix": local, "pid": os.getpid()}) + "\n"
    descriptor = os.open(audit_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, row.encode())
    finally:
        os.close(descriptor)


sys.addaudithook(audit)
# A real permitted connection proves the observer is active in this process.
with socket.create_connection(("127.0.0.1", int(os.environ["DDP_TEST_SOCKET_CONTROL_PORT"])), timeout=2):
    pass

launcher = os.environ["DDP_TEST_REAL_RUNTIME_LAUNCHER"]
sys.argv[0] = launcher
runpy.run_path(launcher, run_name="__main__")
