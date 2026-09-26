"""Single source of truth for workspace SQLite schema versions.

LocalStore (workspace.sqlite3) and ConsentStore (consents.sqlite3) migrate
older databases forward on open. The desktop installer
(scripts/update_check.py) and packaging (scripts/build_desktop.py) must agree
on what the current code can open, or real upgrades refuse real databases.

`0` is the uninitialized file both stores create from; it never persists on a
healthy workspace (fresh databases end at CURRENT), but the current code opens
it, so it is listed here and in the release marker.
"""

WORKSPACE_SCHEMA_VERSIONS = {
    "workspace.sqlite3": (0, 1, 2, 3),
    "consents.sqlite3": (0, 1, 2, 3, 4),
}

WORKSPACE_CURRENT_VERSIONS = {
    "workspace.sqlite3": 3,
    "consents.sqlite3": 4,
}

WORKSPACE_DATABASES = ("workspace.sqlite3", "consents.sqlite3")
