"""Exercise the gate CLI in an isolated checkout with observable tool processes.

No application dependencies, network, database, or browser are used here. The
stubs record which checks actually ran, their cwd, and failures for the real
shell script to aggregate. The normal root pytest run includes these tests.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check.sh"
PACKAGE_DIRS = [
    "python/ddp_core", "python/ddp_local", "services/model-gateway",
    "services/corpus-api", "services/corpus-worker", "services/mcp", "eval",
]
STUB = """#!/bin/sh
name=${0##*/}
printf '%s|%s|%s\\n' "$name" "$PWD" "$*" >> "$CHECK_TEST_LOG"
[ "${CHECK_FAIL:-}" = "$name $*" ] && exit 9
[ "$name" = docker ] && exit 1
exit 0
"""


class Checkout:
    def __init__(self, tmp_path):
        self.root = tmp_path / "checkout"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        for directory in ["scripts", "services/control-api", "apps/web/node_modules",
                          *PACKAGE_DIRS]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.bin.mkdir()
        shutil.copyfile(SCRIPT, self.root / "scripts/check.sh")
        for name in ["python3", "custom-python", "go", "gofmt", "npm", "npx", "node",
                     "docker"]:
            self.executable(self.bin / name)

        # check.sh prepends a user Go installation to PATH. Bash functions keep
        # these tests isolated even when that installation exists on the host;
        # BASH_ENV also covers the script's nested `bash -c` gofmt invocation.
        bash_env = tmp_path / "bash-env"
        bash_env.write_text(
            'go() { "$CHECK_TEST_BIN/go" "$@"; }\n'
            'gofmt() { "$CHECK_TEST_BIN/gofmt" "$@"; }\n',
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env.pop("PY", None)
        self.env.pop("CONTROL_TEST_DATABASE_URL", None)
        # CI 把跳过视为失败：测试各自显式传 CI，这里先清掉 ambient，避免本机/CI 结果不一致。
        self.env.pop("CI", None)
        self.env.update({
            "PATH": f"{self.bin}:{os.defpath}",
            "BASH_ENV": str(bash_env),
            "CHECK_TEST_BIN": str(self.bin),
            "CHECK_TEST_LOG": str(self.log),
            "CHECK_FAIL": "",
        })

    @staticmethod
    def executable(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STUB, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run(self, *targets, env=None, cwd=None):
        return subprocess.run(
            ["bash", str(self.root / "scripts/check.sh"), *targets],
            cwd=cwd or self.root, env={**self.env, **(env or {})},
            capture_output=True, text=True, timeout=15,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [tuple(line.split("|", 2)) for line in self.log.read_text().splitlines()]


@pytest.fixture
def checkout(tmp_path):
    return Checkout(tmp_path)


@pytest.mark.parametrize("targets", [
    ("guardz",), ("guards", "guardz"), ("",), ("web", ""),
    ("--help", "guardz"), ("guards", "--help"),
])
def test_invalid_targets_fail_before_any_check(checkout, targets):
    result = checkout.run(*targets)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "用法" in result.stderr
    assert checkout.calls() == []


def test_help_needs_no_interpreter_and_runs_no_checks(checkout):
    result = checkout.run("--help", env={"PY": "/no/such/python"})
    assert result.returncode == 0, result.stderr
    for target in ["guards", "python", "go", "web", "web-e2e"]:
        assert target in result.stdout
    assert checkout.calls() == []


def test_default_targets_include_build_but_not_browser_tests(checkout):
    result = checkout.run()
    assert result.returncode == 0, result.stdout + result.stderr
    commands = [(name, args) for name, _, args in checkout.calls()]
    assert ("python3", "packages/contracts/scripts/generate.py --check") in commands
    assert ("python3", "-m pytest -q") in commands
    assert ("go", "test ./... -count=1") in commands
    assert ("npm", "run --silent build") in commands
    assert ("node", "--test apps/desktop/test/*.test.mjs") in commands
    assert not any("test:e2e" in args for _, args in commands)


@pytest.mark.parametrize("form", ["command", "relative", "absolute", "caller-relative"])
def test_explicit_python_survives_package_directory_changes(checkout, form):
    interpreter = checkout.bin / "custom-python"
    cwd = checkout.root
    if form == "command":
        value = "custom-python"
    elif form == "relative":
        value = os.path.relpath(interpreter, cwd)
    elif form == "absolute":
        interpreter = checkout.executable(checkout.bin / "with space" / "custom-python")
        value = str(interpreter)
    else:
        cwd = checkout.root.parent
        value = os.path.relpath(interpreter, cwd)
    result = checkout.run("python", env={"PY": value}, cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = checkout.calls()
    assert {name for name, _, _ in calls} == {"custom-python"}
    package_cwds = {cwd for _, cwd, args in calls if args == "-m pytest -q"}
    assert package_cwds == {str(checkout.root / package) for package in PACKAGE_DIRS}


@pytest.mark.parametrize("value", ["", "missing-python-command", "./missing-python",
                                  "/no/such/python", "./scripts"])
def test_invalid_explicit_python_never_falls_back(checkout, value):
    checkout.executable(checkout.root / ".venv/bin/python")
    result = checkout.run("python", env={"PY": value})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "PY" in result.stderr
    assert checkout.calls() == []


def test_nonexecutable_explicit_python_never_falls_back(checkout):
    interpreter = checkout.executable(checkout.bin / "not-executable")
    interpreter.chmod(0o644)
    result = checkout.run("python", env={"PY": str(interpreter)})
    assert result.returncode == 2
    assert checkout.calls() == []


@pytest.mark.parametrize("has_venv", [False, True])
def test_unset_python_prefers_venv_then_path(checkout, has_venv):
    if has_venv:
        checkout.executable(checkout.root / ".venv/bin/python")
    result = checkout.run("python")
    assert result.returncode == 0, result.stdout + result.stderr
    expected = "python" if has_venv else "python3"
    assert {name for name, _, _ in checkout.calls()} == {expected}


def test_database_connection_is_not_logged(checkout):
    url = "postgres://fake-user:fake-password@fake-host:15432/fake-db?sslmode=require"
    result = checkout.run("go", env={"CONTROL_TEST_DATABASE_URL": url})
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "已配置 CONTROL_TEST_DATABASE_URL" in output
    for private_part in [url, "fake-user", "fake-password", "fake-host", "fake-db"]:
        assert private_part not in output


def test_unset_database_connection_is_reported(checkout):
    result = checkout.run("go")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "没有 CONTROL_TEST_DATABASE_URL" in result.stdout
    assert "跳过" in result.stdout
    assert "scripts/dev.sh up postgres" not in result.stdout


def test_web_build_failure_is_aggregated_and_later_checks_still_run(checkout):
    result = checkout.run("web", env={"CHECK_FAIL": "npm run --silent build"})
    assert result.returncode != 0
    commands = [(name, args) for name, _, args in checkout.calls()]
    assert commands[0] == ("npm", "run --silent build")
    assert ("npx", "vitest run") in commands
    assert ("node", "--test apps/desktop/test/*.test.mjs") in commands
    assert not any("type-check" in args for _, args in commands)
    assert "FAIL" in result.stdout and "失败 1" in result.stdout


def test_failed_python_check_does_not_hide_later_groups(checkout):
    result = checkout.run("python", "web", env={
        "CHECK_FAIL": "python3 -m ruff check . --select F,B --ignore F401,B008,B905,B904,B007",
    })
    assert result.returncode != 0
    assert ("npm", str(checkout.root / "apps/web"), "run --silent build") in checkout.calls()
    assert "FAIL" in result.stdout and "失败 1" in result.stdout


@pytest.mark.parametrize("fail", [False, True])
def test_browser_target_is_explicit_and_propagates_failures(checkout, fail):
    result = checkout.run("web-e2e", env={
        "CHECK_FAIL": "npm run --silent test:e2e" if fail else "",
    })
    assert (result.returncode != 0) == fail
    assert checkout.calls() == [
        ("npm", str(checkout.root / "apps/web"), "run --silent test:e2e"),
    ]


@pytest.mark.parametrize("target", ["web", "web-e2e"])
def test_missing_web_dependencies_fail_loudly(checkout, target):
    (checkout.root / "apps/web/node_modules").rmdir()
    result = checkout.run(target)
    assert result.returncode != 0
    assert "依赖未安装" in result.stdout
    assert checkout.calls() == []

def test_default_run_reports_database_skips_and_stays_green(checkout):
    # 桩 docker 恒 exit 1（边界走跳过）+ 未设 CONTROL_TEST_DATABASE_URL（计量走跳过）。
    result = checkout.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" in result.stdout
    assert "数据所有权（真库）：dev postgres 未起，已跳过" in result.stdout
    assert "计量用例（需 CONTROL_TEST_DATABASE_URL）：4 条，已跳过" in result.stdout
    assert "--allow-without-db" in result.stdout
    assert "跳过 2" in result.stdout and "失败 0" in result.stdout


def test_ci_turns_database_skips_into_failures(checkout):
    result = checkout.run(env={"CI": "true"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SKIP" not in result.stdout
    assert "FAIL" in result.stdout
    assert "数据所有权（真库）：dev postgres 未起，已跳过" in result.stdout
    assert "计量用例（需 CONTROL_TEST_DATABASE_URL）：4 条，已跳过" in result.stdout
    assert "跳过 0" in result.stdout and "失败 2" in result.stdout


def test_ci_with_allow_without_db_keeps_skips_visible(checkout):
    result = checkout.run("--allow-without-db", env={"CI": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" in result.stdout
    assert "数据所有权（真库）：dev postgres 未起，已跳过" in result.stdout
    assert "计量用例（需 CONTROL_TEST_DATABASE_URL）：4 条，已跳过" in result.stdout
    assert "跳过 2" in result.stdout and "失败 0" in result.stdout


def test_allow_without_db_is_a_flag_not_a_target(checkout):
    result = checkout.run("guards", "--allow-without-db", env={"CI": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" in result.stdout and "跳过 1" in result.stdout
