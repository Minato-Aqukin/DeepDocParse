"""The release gate must reject incomplete acceptance, not merely valid references."""

import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_acceptance_matrix.py"


def test_release_requires_complete_acceptance_even_when_development_guard_passes(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs/refactor").mkdir(parents=True)
    script = tmp_path / "scripts/check_acceptance_matrix.py"
    shutil.copyfile(SCRIPT, script)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    # Deliberately untracked: the working-tree gate must not require a commit first.
    (tmp_path / "test_source.py").write_text(
        "def test_artifact_origin():\n"
        "    raise AssertionError('parser fixture must not be executed')\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "docs/refactor/ACCEPTANCE-MATRIX-v3.md"
    rows = []
    for number in range(1, 89):
        status = "⛔" if number == 47 else "✅"
        gap = "等待人工复核" if number == 47 else "—"
        rows.append(
            f"| T{number:02d} | acceptance | {status} | "
            f"`test_source.py::test_artifact_origin` | {gap} |"
        )
    matrix.write_text(
        "<!-- counts:begin -->\n\n<!-- counts:end -->\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )

    def run(*args):
        return subprocess.run(
            [sys.executable, str(script), *args], cwd=tmp_path,
            capture_output=True, text=True, timeout=15,
        )

    assert run("--write").returncode == 0
    development = run()
    assert development.returncode == 0, development.stdout + development.stderr
    release = run("--require-complete")
    assert release.returncode == 1
    assert "T47" in release.stdout
    assert run("--require-complete", "--write").returncode == 2

    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace("⛔ |", "✅ |").replace("等待人工复核", "—"),
        encoding="utf-8",
    )
    assert run("--write").returncode == 0
    completed = run("--require-complete")
    assert completed.returncode == 0, completed.stdout + completed.stderr
