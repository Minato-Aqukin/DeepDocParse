"""发布链最小闭环的行为测试（T-007 R1）。

覆盖验收 A–F，不止 YAML 形状：用 fake gh（可执行脚本，argv 全记录）
与临时小构件走完整 CLI 流程；坏样本逐个断言失败码；命令注入 marker 进了
输入必须被拒且不执行。构件 ZIP 用真实层级（windows/ + 根归档），不是
扁平 fixture。

铁律：本文件任何测试都不许触碰真实网络与真实 ``gh``——CLI 测试一律传
``--gh <fake绝对路径>``，绝不依赖 PATH 上的 ``gh``。helper 本身除 ``--gh``
指向的程序外不执行任何外部命令（有 AST 断言钉住）。
"""

import ast
import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "release_publication.py"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
DESKTOP_YML = REPO_ROOT / ".github" / "workflows" / "desktop-windows.yml"

_spec = importlib.util.spec_from_file_location("release_publication", HELPER)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

REPO = "Minato-Aqukin/DeepDocParse"
REPO_ID = 1299090901
RUN_ID = 35233429350
SHA = "e0002f9fa1bb28e43bbb6499ccf7dc9fa3b52127"
SHA2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
VERSION = "0.1.0"
TAG = "v0.1.0"
SETUP_NAME = f"DeepDocParse-{VERSION}-win-x64-setup.exe"
PORTABLE_NAME = f"DeepDocParse-{VERSION}-win-x64-portable.exe"
ARTIFACTS_URL = f"repos/{REPO}/actions/runs/{RUN_ID}/artifacts?per_page=100&page=1"
REFS_URL = f"repos/{REPO}/git/refs/tags/{TAG}"
ZIP_URL = f"repos/{REPO}/actions/artifacts/10502485781/zip"

FAKE_GH = '''#!/usr/bin/env python3
"""Fake gh: canned API responses + argv log. No network, no side effects."""
import base64
import io
import json
import os
import sys
import zipfile

state = json.load(open(os.environ["FAKE_GH_STATE"], encoding="utf-8"))
log = os.environ.get("FAKE_GH_LOG", "")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]


def emit_api(endpoint, include):
    if endpoint.endswith("/zip"):
        if "raw_zip_b64" in state:
            sys.stdout.buffer.write(base64.b64decode(state["raw_zip_b64"]))
            return 0
        members = state.get("zip", {})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, content in members.items():
                if isinstance(content, dict) and "__bytes__" in content:
                    content = base64.b64decode(content["__bytes__"])
                archive.writestr(name, content)
        sys.stdout.buffer.write(buf.getvalue())
        return 0
    entry = state.get("api", {}).get(endpoint)
    if entry is None:
        sys.stderr.write(f"fake-gh: unexpected endpoint {endpoint}\\n")
        return 1
    if isinstance(entry, dict) and "__error__" in entry:
        err = entry["__error__"]
        if include:
            sys.stdout.write(
                f"HTTP/1.1 {err.get('status', 500)} Error\\r\\n"
                f"Content-Type: application/json\\r\\n\\r\\n{{}}")
        sys.stderr.write(err.get("stderr", "fake-gh error") + "\\n")
        return err.get("code", 1)
    if isinstance(entry, dict) and "__body__" in entry:
        status, body = entry.get("__status__", 200), entry["__body__"]
    else:
        status, body = 200, entry
    payload = json.dumps(body)
    if include:
        sys.stdout.write(
            f"HTTP/1.1 {status} OK\\r\\nContent-Type: application/json\\r\\n\\r\\n" + payload)
    else:
        sys.stdout.write(payload)
    return 0


if args[0] == "api":
    include = len(args) > 2 and args[1] == "--include"
    endpoint = args[2] if include else args[1]
    sys.exit(emit_api(endpoint, include))
if args[:2] == ["release", "create"]:
    rest = args[2:]
    # 选项边界与 repo 钉死：--repo 必须出现且值正确；-- 之后不许再有选项。
    assert rest[0] == "--repo", f"repo not pinned first: {rest}"
    assert rest[1] == state.get("expect_repo", "UNSET"), f"wrong repo: {rest[1]}"
    dash = rest.index("--")
    assert rest[2] == state.get("expect_tag", "UNSET"), f"wrong tag: {rest[2:4]}"
    for token in rest[dash + 1:]:
        assert not token.startswith("-"), f"option after --: {token}"
    saved = json.load(open(os.environ["FAKE_GH_STATE"], encoding="utf-8"))
    saved.setdefault("releases", []).append(rest)
    json.dump(saved, open(os.environ["FAKE_GH_STATE"], "w", encoding="utf-8"))
    sys.stdout.write("fake release created\\n")
    sys.exit(0)
sys.stderr.write(f"fake-gh: unexpected command {args}\\n")
sys.exit(1)
'''


def make_fake_gh(tmp_path, state):
    fake = tmp_path / "gh"
    fake.write_text(FAKE_GH, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    zip_members = state.get("zip", {})
    encoded = {name: {"__bytes__": base64.b64encode(content).decode("ascii")}
               for name, content in zip_members.items()}
    state = dict(state, zip=encoded)
    state_file = tmp_path / "gh-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    log_file = tmp_path / "gh-log.jsonl"
    # 函数级测试直接 in-process 调 helper，子进程继承 os.environ；
    # 这里直接写入（每用例独立 tmp 路径，不串扰），CLI 测试另传 env 也一致。
    os.environ["FAKE_GH_STATE"] = str(state_file)
    os.environ["FAKE_GH_LOG"] = str(log_file)
    env = dict(os.environ)
    return str(fake), env, state_file, log_file


def gh_log_entries(log_file):
    path = Path(log_file)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def good_run(**overrides):
    run = {
        "id": RUN_ID,
        "path": ".github/workflows/desktop-windows.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": SHA,
        "run_attempt": 1,
        "pull_requests": [],
        "repository": {"full_name": REPO, "id": REPO_ID},
        "head_repository": {"full_name": REPO, "id": REPO_ID},
    }
    run.update(overrides)
    return run


def version_blob(version=VERSION):
    payload = base64.b64encode(json.dumps({"version": version}).encode()).decode()
    return {"encoding": "base64", "content": payload}


def run_endpoints(run, version=VERSION):
    return {
        f"repos/{REPO}/actions/runs/{RUN_ID}": run,
        f"repos/{REPO}/contents/apps/desktop/package.json?ref={SHA}": version_blob(version),
    }


def artifact_entry(name="windows-installers", expired=False, sha=SHA, aid=10502485781,
                   run_id=RUN_ID, repo_id=REPO_ID, branch="main"):
    return {
        "id": aid,
        "name": name,
        "expired": expired,
        "digest": "sha256:84242ebab0e3de2d966024d0901268dd64e9460dc9dcf9f72ee8ba7e3f13e133",
        # 故意放一个外链：helper 必须用校验过的 ID 构造同仓固定路径下载，
        # 碰这个 URL 就是缺陷（测试断言实际调用的 endpoint）。
        "archive_download_url": "https://evil.example.net/artifacts/10502485781/zip",
        "workflow_run": {"id": run_id, "repository_id": repo_id,
                         "head_repository_id": repo_id,
                         "head_branch": branch, "head_sha": sha},
    }


def artifacts_page(items, total=None):
    return {"total_count": total if total is not None else len(items), "artifacts": items}


def make_package(pkg_root, version=VERSION, exe_bytes=b"x" * 4096):
    """真实层级小构件：windows/ 下 setup+portable+sidecar+清单；根留历史 release.json。"""
    windows = Path(pkg_root) / "windows"
    windows.mkdir(parents=True, exist_ok=True)
    setup = windows / f"DeepDocParse-{version}-win-x64-setup.exe"
    portable = windows / f"DeepDocParse-{version}-win-x64-portable.exe"
    setup.write_bytes(b"setup:" + exe_bytes)
    portable.write_bytes(b"portable:" + exe_bytes)
    lines = []
    for path in (setup, portable):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (windows / (path.name + ".sha256")).write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        lines.append(f"{digest}  {path.name}")
    (windows / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (Path(pkg_root) / f"deepdocparse-{version}-win32-x64.release.json").write_text(
        json.dumps({"version": version, "platform": "win32-x64"}), encoding="utf-8")
    return setup, portable


def write_receipt_for(pkg_root, version=VERSION, run_id=RUN_ID, attempt=1, sha=SHA, direct=True):
    package_json = Path(pkg_root) / "package.json"
    package_json.write_text(json.dumps({"version": version}), encoding="utf-8")
    windows = Path(pkg_root) / "windows"
    base = str(windows if direct else pkg_root)
    return rp.write_receipt(
        base, str(package_json), REPO, str(run_id), str(attempt), sha,
        str(windows / "source-receipt.json"),
    )


def package_members(pkg_root):
    """构件 ZIP 成员：带真实层级；package.json 只是 receipt 输入，不进包。"""
    members = {}
    for path in sorted(Path(pkg_root).rglob("*")):
        if path.is_file() and path.name != "package.json":
            members[str(path.relative_to(pkg_root)).replace(os.sep, "/")] = path.read_bytes()
    return members


def good_source(**overrides):
    source = {"repo": REPO, "run_id": RUN_ID, "run_attempt": 1,
              "head_sha": SHA, "version": VERSION,
              "repo_id": REPO_ID, "head_repo_id": REPO_ID}
    source.update(overrides)
    return source


# ---------------------------------------------------------------- A. 输入校验


class TestInputs:
    @pytest.mark.parametrize("bad", ["", "0", "-1", "01", "1;rm -rf", "$(touch X)",
                                     "`id`", "1abc", "3.5", " 1", "1 ", "9" * 30])
    def test_bad_run_id_rejected(self, bad):
        with pytest.raises(rp.PublicationError, match="BAD_RUN_ID"):
            rp.validate_run_id(bad)

    def test_good_run_id(self):
        assert rp.validate_run_id("35233429350") == 35233429350

    @pytest.mark.parametrize("bad", ["", "1.0.0", "v1", "v1.0", "v1.0.0.0", "-v1.0.0",
                                     "v1.0.0;evil", "v1.0.0$(id)", "v0.1.0-rc1",
                                     "v0.1.0 ", "../v1.0.0", "--help"])
    def test_bad_tag_rejected(self, bad):
        with pytest.raises(rp.PublicationError, match="BAD_TAG"):
            rp.validate_tag(bad)

    def test_good_tag(self):
        assert rp.validate_tag("v0.1.0") == "v0.1.0"

    def test_assert_ref_main_ok(self):
        assert rp.assert_trusted_ref("refs/heads/main") == "refs/heads/main"

    @pytest.mark.parametrize("ref", ["refs/heads/other", "refs/pull/1/merge",
                                     "refs/tags/v0.1.0", "", "main"])
    def test_assert_ref_rejects(self, ref):
        with pytest.raises(rp.PublicationError, match="UNTRUSTED_REF"):
            rp.assert_trusted_ref(ref)


# ---------------------------------------------------------------- A. 来源核验


class TestVerifyRun:
    def run_source(self, tmp_path, run, version=VERSION, ref="refs/heads/main",
                   run_id="35233429350"):
        fake, env, _, _ = make_fake_gh(tmp_path, {"api": run_endpoints(run, version)})
        return rp.verify_run([fake], REPO, run_id, ref)

    def test_good_run(self, tmp_path):
        assert self.run_source(tmp_path, good_run()) == good_source()

    @pytest.mark.parametrize("mutation,code", [
        ({"head_branch": "other"}, "SOURCE_BRANCH_MISMATCH"),
        ({"path": ".github/workflows/other.yml"}, "SOURCE_WORKFLOW_MISMATCH"),
        ({"path": ".github/workflows/desktop-windows.yml@main"}, "SOURCE_WORKFLOW_MISMATCH"),
        ({"event": "pull_request"}, "SOURCE_EVENT_REJECTED"),
        ({"event": "schedule"}, "SOURCE_EVENT_REJECTED"),
        ({"status": "in_progress"}, "SOURCE_NOT_COMPLETED"),
        ({"conclusion": "failure"}, "SOURCE_NOT_SUCCESS"),
        ({"conclusion": "cancelled"}, "SOURCE_NOT_SUCCESS"),
        ({"pull_requests": [{"id": 1}]}, "SOURCE_HAS_PRS"),
        ({"head_sha": "zzz"}, "BAD_SHA"),
        ({"run_attempt": 0}, "SOURCE_BAD_ATTEMPT"),
        ({"run_attempt": True}, "BAD_FIELD_TYPE"),
        ({"id": 999}, "SOURCE_ID_MISMATCH"),
        ({"status": 200}, "BAD_FIELD_TYPE"),
    ])
    def test_bad_runs_rejected(self, tmp_path, mutation, code):
        with pytest.raises(rp.PublicationError, match=code):
            self.run_source(tmp_path, good_run(**mutation))

    def test_missing_pull_requests_is_not_ok(self, tmp_path):
        # R-203 反例：缺 pull_requests 键不得当作"无 PR"放行。
        run = good_run()
        del run["pull_requests"]
        with pytest.raises(rp.PublicationError, match="SOURCE_HAS_PRS"):
            self.run_source(tmp_path, run)

    def test_fork_head_repo_rejected(self, tmp_path):
        run = good_run()
        run["head_repository"] = {"full_name": "Evil/Fork", "id": 7}
        with pytest.raises(rp.PublicationError, match="SOURCE_FORK"):
            self.run_source(tmp_path, run)

    def test_other_repo_rejected(self, tmp_path):
        run = good_run()
        run["repository"] = {"full_name": "Evil/Other", "id": 7}
        with pytest.raises(rp.PublicationError, match="SOURCE_REPO_MISMATCH"):
            self.run_source(tmp_path, run)

    def test_missing_field_rejected(self, tmp_path):
        run = good_run()
        del run["conclusion"]
        with pytest.raises(rp.PublicationError, match="SOURCE_FIELD_MISSING"):
            self.run_source(tmp_path, run)

    def test_missing_repo_ids_rejected(self, tmp_path):
        run = good_run()
        del run["repository"]["id"]
        with pytest.raises(rp.PublicationError, match="BAD_FIELD_TYPE"):
            self.run_source(tmp_path, run)

    def test_untrusted_ref_rejected_before_api(self, tmp_path):
        fake, env, _, log = make_fake_gh(tmp_path, {"api": run_endpoints(good_run())})
        with pytest.raises(rp.PublicationError, match="UNTRUSTED_REF"):
            rp.verify_run([fake], REPO, "35233429350", "refs/heads/other")
        assert gh_log_entries(log) == []

    def test_injection_run_id_never_reaches_gh(self, tmp_path):
        marker = tmp_path / "PWNED"
        fake, env, _, log = make_fake_gh(tmp_path, {"api": run_endpoints(good_run())})
        with pytest.raises(rp.PublicationError, match="BAD_RUN_ID"):
            rp.verify_run([fake], REPO, f"1$(touch {marker})", "refs/heads/main")
        assert gh_log_entries(log) == []
        assert not marker.exists()

    def test_version_anchor_from_source_sha(self, tmp_path):
        source = self.run_source(tmp_path, good_run(), version="0.2.1")
        assert source["version"] == "0.2.1"

    def test_version_anchor_unavailable(self, tmp_path):
        endpoints = run_endpoints(good_run())
        endpoints[f"repos/{REPO}/contents/apps/desktop/package.json?ref={SHA}"] = {
            "__error__": {"code": 1, "stderr": "gh: Not Found (HTTP 404)"}}
        fake, env, _, _ = make_fake_gh(tmp_path, {"api": endpoints})
        with pytest.raises(rp.PublicationError, match="VERSION_ANCHOR_UNAVAILABLE"):
            rp.verify_run([fake], REPO, "35233429350", "refs/heads/main")

    def test_tag_must_match_source_version(self):
        with pytest.raises(rp.PublicationError, match="TAG_VERSION_MISMATCH"):
            rp.check_tag_matches_version("v9.9.9", VERSION)
        assert rp.check_tag_matches_version(TAG, VERSION) == TAG


# ---------------------------------------------------------------- B. 构件获取


class TestFetchArtifact:
    def test_good_download_by_id(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry()])},
                 "zip": package_members(pkg)}
        fake, env, _, log = make_fake_gh(tmp_path, state)
        dest = tmp_path / "dest"
        info = rp.fetch_artifact([fake], REPO, good_source(), str(dest))
        assert info["artifact_id"] == 10502485781
        assert (dest / "windows" / "SHA256SUMS").exists()
        # 根归档随包落地，但校验锚定 windows/。
        assert (dest / f"deepdocparse-{VERSION}-win32-x64.release.json").exists()
        calls = gh_log_entries(log)
        # 按校验过的 ID 构造同仓固定路径下载；元信息里的外链绝不碰。
        assert [ZIP_URL] == [c[1] for c in calls if c[0] == "api" and c[1].endswith("/zip")]
        assert not any("evil.example.net" in json.dumps(c) for c in calls)
        assert not any(c[:2] == ["run", "download"] for c in calls)

    def test_pagination_finds_unique(self, tmp_path):
        other = artifact_entry(name="other", aid=1)
        target = artifact_entry(aid=2)
        stale = artifact_entry(aid=3, expired=True)
        state = {"api": {
            ARTIFACTS_URL: artifacts_page([other, stale], total=3),
            ARTIFACTS_URL.replace("&page=1", "&page=2"): artifacts_page([target], total=3),
        }, "zip": {"windows/f": b"data"}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        info = rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))
        assert info["artifact_id"] == 2

    def test_mid_empty_page_rejected(self, tmp_path):
        # R-203 反例：total=2 但第二页空，不得把第一页那条拿去下载。
        state = {"api": {
            ARTIFACTS_URL: artifacts_page([artifact_entry()], total=2),
            ARTIFACTS_URL.replace("&page=1", "&page=2"): artifacts_page([], total=2),
        }, "zip": {"windows/f": b"data"}}
        fake, env, _, log = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_LIST_INCOMPLETE"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))
        assert not any(c[1].endswith("/zip") for c in gh_log_entries(log) if c[0] == "api")

    def test_total_change_rejected(self, tmp_path):
        state = {"api": {
            ARTIFACTS_URL: artifacts_page([artifact_entry()], total=2),
            ARTIFACTS_URL.replace("&page=1", "&page=2"):
                artifacts_page([artifact_entry(aid=9)], total=3),
        }, "zip": {}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_LIST_CHANGED"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))

    def test_duplicate_ids_rejected(self, tmp_path):
        state = {"api": {
            ARTIFACTS_URL: artifacts_page([artifact_entry(aid=5), artifact_entry(aid=5)], total=2),
        }}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_LIST_DUPLICATE"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))

    def test_ambiguous_rejected(self, tmp_path):
        state = {"api": {
            ARTIFACTS_URL: artifacts_page([artifact_entry(aid=1), artifact_entry(aid=2)]),
        }}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_AMBIGUOUS"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))

    def test_expired_only_rejected(self, tmp_path):
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry(expired=True)])}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_MISSING"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))

    @pytest.mark.parametrize("unknown", ["missing", None, "false", "False", 0, 1, ""])
    def test_live_plus_unknown_expired_fails(self, tmp_path, unknown):
        # R-203 终审反例：{live A, unknown B} 不得静默压成 [A] 下载。
        live = artifact_entry(aid=1)
        other = artifact_entry(aid=2)
        if unknown == "missing":
            del other["expired"]
        else:
            other["expired"] = unknown
        state = {"api": {ARTIFACTS_URL: artifacts_page([live, other], total=2)}}
        fake, env, _, log = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_EXPIRED_UNKNOWN"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))
        assert not any(c[1].endswith("/zip") for c in gh_log_entries(log) if c[0] == "api")

    def test_live_plus_explicit_expired_ok(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        state = {"api": {ARTIFACTS_URL: artifacts_page(
            [artifact_entry(aid=1), artifact_entry(aid=2, expired=True)], total=2)},
            "zip": package_members(pkg)}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        info = rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))
        assert info["artifact_id"] == 1

    def test_artifact_sha_mismatch_rejected(self, tmp_path):
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry(sha=SHA2)])}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="ARTIFACT_SHA_MISMATCH"):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))

    @pytest.mark.parametrize("field,code", [
        ({"run_id": 999}, "ARTIFACT_RUN_MISMATCH"),
        ({"repo_id": 1}, "ARTIFACT_REPO_MISMATCH"),
        ({"branch": "other"}, "ARTIFACT_BRANCH_MISMATCH"),
    ])
    def test_artifact_workflow_run_must_match_source(self, tmp_path, field, code):
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry(**field)])}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match=code):
            rp.fetch_artifact([fake], REPO, good_source(), str(tmp_path / "d"))


# ---------------------------------------------------------------- B2. ZIP 解压安全


def build_raw_zip(members):
    """手造 zip（含 symlink/dup/坏路径成员）：[(name, data, mode|None)]。"""
    import io
    import warnings
    import zipfile
    buf = io.BytesIO()
    with warnings.catch_warnings():
        # 故意造重名成员测预检：3.14 写时即警告，这里按住（被测行为在读侧）。
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf, "w") as archive:
            for name, data, mode in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                if mode is not None:
                    info.external_attr = (mode & 0o177777) << 16
                archive.writestr(info, data)
    return buf.getvalue()


class TestZipSafety:
    def fetch_with_raw(self, tmp_path, raw, dest=None):
        import base64 as b64
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry()])},
                 "raw_zip_b64": b64.b64encode(raw).decode("ascii")}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        return rp.fetch_artifact([fake], REPO, good_source(), str(dest or tmp_path / "d"))

    def test_zip_slip_rejected(self, tmp_path):
        raw = build_raw_zip([("windows/ok.exe", b"data", 0o100644),
                             ("../evil", b"x", 0o100644)])
        with pytest.raises(rp.PublicationError, match="ARTIFACT_ZIP_SLIP"):
            self.fetch_with_raw(tmp_path, raw)

    def test_absolute_and_windows_paths_rejected(self, tmp_path):
        for bad in ("/abs.exe", "C:\\win.exe", "C:/win.exe"):
            raw = build_raw_zip([(bad, b"x", 0o100644)])
            with pytest.raises(rp.PublicationError, match="ARTIFACT_ZIP_SLIP"):
                self.fetch_with_raw(tmp_path, raw)

    def test_symlink_member_rejected(self, tmp_path):
        import stat as statmod
        raw = build_raw_zip([("windows/link.exe", b"target", statmod.S_IFLNK | 0o777)])
        outside = tmp_path / "ORIGINAL"
        outside.write_text("ORIGINAL")
        with pytest.raises(rp.PublicationError, match="ARTIFACT_ZIP_SYMLINK"):
            self.fetch_with_raw(tmp_path, raw)
        assert outside.read_text() == "ORIGINAL"

    def test_duplicate_member_rejected(self, tmp_path):
        raw = build_raw_zip([("windows/a.exe", b"1", 0o100644),
                             ("windows/a.exe", b"2", 0o100644)])
        with pytest.raises(rp.PublicationError, match="ARTIFACT_ZIP_DUPLICATE"):
            self.fetch_with_raw(tmp_path, raw)

    def test_dest_preset_symlink_blocks_write(self, tmp_path):
        # R-205 反例：dest 里预置软链指目录外，解包不得跟随改写外部文件。
        outside = tmp_path / "ORIGINAL"
        outside.write_text("ORIGINAL")
        dest = tmp_path / "d"
        dest.mkdir()
        (dest / "windows").mkdir()
        (dest / "windows" / SETUP_NAME).symlink_to(outside)
        pkg = tmp_path / "pkg"
        make_package(pkg)
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry()])},
                 "zip": package_members(pkg)}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="DEST_NOT_CLEAN"):
            rp.fetch_artifact([fake], REPO, good_source(), str(dest))
        assert outside.read_text() == "ORIGINAL"

    def test_dest_itself_symlink_rejected(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        dest = tmp_path / "d"
        dest.symlink_to(real)
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry()])},
                 "zip": {"windows/f": b"data"}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="DEST_NOT_CLEAN"):
            rp.fetch_artifact([fake], REPO, good_source(), str(dest))

    def test_nonempty_dest_rejected(self, tmp_path):
        dest = tmp_path / "d"
        dest.mkdir()
        (dest / "stale.txt").write_text("stale")
        state = {"api": {ARTIFACTS_URL: artifacts_page([artifact_entry()])},
                 "zip": {"windows/f": b"data"}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="DEST_NOT_CLEAN"):
            rp.fetch_artifact([fake], REPO, good_source(), str(dest))


# ---------------------------------------------------------------- C. 安装包校验


class TestVerifyPackage:
    def good_dir(self, tmp_path, name="pkg", **kw):
        pkg = tmp_path / name
        make_package(pkg, version=kw.get("version", VERSION))
        write_receipt_for(pkg, **kw)
        return pkg

    def test_roundtrip_ok(self, tmp_path):
        summary = rp.verify_package(str(self.good_dir(tmp_path)), good_source(), TAG)
        assert summary["version"] == VERSION and summary["head_sha"] == SHA

    def test_root_mode_receipt_ok(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        write_receipt_for(pkg, direct=False)
        assert rp.verify_package(str(pkg), good_source(), TAG)["version"] == VERSION

    def test_flat_layout_rejected(self, tmp_path):
        # R-201 反例：扁平 fixture（exe 在根）必须锚定失败，真构件是 windows/ 层级。
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / SETUP_NAME).write_bytes(b"x" * 100)
        with pytest.raises(rp.PublicationError, match="PACKAGE_LAYOUT"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_stray_candidate_rejected(self, tmp_path):
        pkg = self.good_dir(tmp_path)
        (pkg / SETUP_NAME).write_bytes(b"stray")
        with pytest.raises(rp.PublicationError, match="PACKAGE_STRAY_CANDIDATE"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_wrong_version_filename_rejected(self, tmp_path):
        # R-207 反例：9.9.9 文件名 + 全套自洽哈希，配 0.1.0 来源/tag。
        pkg = tmp_path / "pkg"
        make_package(pkg, version="9.9.9")
        # 生成侧：package.json 0.1.0 配 9.9.9 文件名 → 生成即拒。
        (pkg / "package.json").write_text(json.dumps({"version": VERSION}))
        with pytest.raises(rp.PublicationError, match="RECEIPT_FILENAME_MISMATCH"):
            rp.write_receipt(str(pkg / "windows"), str(pkg / "package.json"), REPO,
                             str(RUN_ID), "1", SHA,
                             str(pkg / "windows" / "source-receipt.json"))
        # 校验侧：9.9.9 全套（哈希自洽）配 0.1.0 来源 → 校验拒。
        (pkg / "package.json").write_text(json.dumps({"version": "9.9.9"}))
        rp.write_receipt(str(pkg / "windows"), str(pkg / "package.json"), REPO,
                         str(RUN_ID), "1", SHA,
                         str(pkg / "windows" / "source-receipt.json"))
        with pytest.raises(rp.PublicationError, match="PACKAGE_FILENAME_MISMATCH"):
            rp.verify_package(str(pkg), good_source(), TAG)

    @pytest.mark.parametrize("breakage,code", [
        ("missing_portable", "PACKAGE_FILE_MISSING"),
        ("missing_sidecar", "PACKAGE_FILE_MISSING"),
        ("missing_receipt", "PACKAGE_FILE_MISSING"),
        ("missing_sums", "PACKAGE_FILE_MISSING"),
        ("second_setup", "PACKAGE_FILE_AMBIGUOUS"),
        ("extra_sums_line", "SUMS_COVERAGE"),
        ("sums_missing_line", "SUMS_COVERAGE"),
        ("sums_duplicate", "SUMS_DUPLICATE"),
        ("sums_bad_hash", "PACKAGE_HASH_MISMATCH"),
        ("sums_traversal", "HASH_PATH_TRAVERSAL"),
        ("sidecar_mismatch", "PACKAGE_HASH_MISMATCH"),
        ("sidecar_wrong_name", "SIDECAR_NAME_MISMATCH"),
        ("receipt_no_schema", "RECEIPT_SCHEMA"),
        ("tampered_exe", "PACKAGE_HASH_MISMATCH"),
    ])
    def test_bad_packages_rejected(self, tmp_path, breakage, code):
        pkg = self.good_dir(tmp_path)
        windows = pkg / "windows"
        setup = windows / SETUP_NAME
        portable = windows / PORTABLE_NAME
        if breakage == "missing_portable":
            portable.unlink()
        elif breakage == "missing_sidecar":
            (windows / (setup.name + ".sha256")).unlink()
        elif breakage == "missing_receipt":
            (windows / "source-receipt.json").unlink()
        elif breakage == "missing_sums":
            (windows / "SHA256SUMS").unlink()
        elif breakage == "second_setup":
            (windows / "DeepDocParse-9.9.9-win-x64-setup.exe").write_bytes(b"extra")
        elif breakage == "extra_sums_line":
            with open(windows / "SHA256SUMS", "a", encoding="utf-8") as f:
                f.write("0" * 64 + "  extra.dll\n")
        elif breakage == "sums_missing_line":
            lines = (windows / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            (windows / "SHA256SUMS").write_text(lines[0] + "\n", encoding="utf-8")
        elif breakage == "sums_duplicate":
            lines = (windows / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            (windows / "SHA256SUMS").write_text("\n".join(lines + lines[:1]) + "\n", encoding="utf-8")
        elif breakage == "sums_bad_hash":
            lines = (windows / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            (windows / "SHA256SUMS").write_text("f" * 64 + "  " + lines[0].split()[1] + "\n" + lines[1] + "\n",
                                                encoding="utf-8")
        elif breakage == "sums_traversal":
            lines = (windows / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            (windows / "SHA256SUMS").write_text(lines[0] + "\n" + "0" * 64 + "  ../evil.exe\n", encoding="utf-8")
        elif breakage == "sidecar_mismatch":
            (windows / (setup.name + ".sha256")).write_text("f" * 64 + f"  {setup.name}\n", encoding="utf-8")
        elif breakage == "sidecar_wrong_name":
            (windows / (setup.name + ".sha256")).write_text(
                (windows / (setup.name + ".sha256")).read_text(encoding="utf-8").replace(setup.name, portable.name),
                encoding="utf-8")
        elif breakage == "receipt_no_schema":
            receipt = json.loads((windows / "source-receipt.json").read_text(encoding="utf-8"))
            del receipt["schema"]
            (windows / "source-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        elif breakage == "tampered_exe":
            with open(setup, "ab") as f:
                f.write(b"tamper")
        with pytest.raises(rp.PublicationError, match=code):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_receipt_without_rebuild_is_rejected(self, tmp_path):
        """旧构件无来源清单：明确拒绝，不提供绕过。"""
        pkg = tmp_path / "pkg"
        make_package(pkg)
        with pytest.raises(rp.PublicationError, match="PACKAGE_FILE_MISSING"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_receipt_attempt_mismatch_rejected(self, tmp_path):
        pkg = self.good_dir(tmp_path, attempt=2)
        with pytest.raises(rp.PublicationError, match="RECEIPT_SOURCE_MISMATCH"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_receipt_sha_mismatch_rejected(self, tmp_path):
        pkg = self.good_dir(tmp_path, sha=SHA2)
        with pytest.raises(rp.PublicationError, match="RECEIPT_SOURCE_MISMATCH"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_receipt_version_mismatch_rejected(self, tmp_path):
        # 文件名已绑定版本：0.2.1 文件配 0.1.0 来源先红文件名门。
        pkg = self.good_dir(tmp_path, "v021", version="0.2.1")
        with pytest.raises(rp.PublicationError, match="PACKAGE_FILENAME_MISMATCH"):
            rp.verify_package(str(pkg), good_source(), "v0.2.1")
        # receipt 内容版本被改（文件名仍 0.1.0）→ 逐字段对照门红。
        pkg = self.good_dir(tmp_path, "v010")
        receipt_path = pkg / "windows" / "source-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["version"] = "0.2.1"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with pytest.raises(rp.PublicationError, match="RECEIPT_SOURCE_MISMATCH"):
            rp.verify_package(str(pkg), good_source(), TAG)

    def test_symlink_exe_rejected(self, tmp_path):
        pkg = self.good_dir(tmp_path)
        windows = pkg / "windows"
        setup = windows / SETUP_NAME
        target = windows / "real-setup.exe"
        setup.rename(target)
        setup.symlink_to(target.name)
        with pytest.raises(rp.PublicationError, match="PACKAGE_FILE_MISSING"):
            rp.verify_package(str(pkg), good_source(), TAG)


# ---------------------------------------------------------------- D. tag 解析


class TestResolveTag:
    def test_missing_tag_means_create(self, tmp_path):
        state = {"api": {REFS_URL: {"__error__": {"code": 1, "status": 404,
                                                  "stderr": "gh: Not Found (HTTP 404)"}}}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        assert rp.resolve_tag([fake], REPO, TAG, SHA) == {"action": "create", "tag": TAG, "target": SHA}

    def test_403_with_404_in_trace_is_not_create(self, tmp_path):
        # R-204 反例：stderr 含 404cafe 但 HTTP 状态是 403，必须拒绝而非新建。
        state = {"api": {REFS_URL: {"__error__": {"code": 1, "status": 403,
                                                  "stderr": "HTTP 403 Forbidden; trace id 404cafe"}}}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="TAG_LOOKUP_FAILED"):
            rp.resolve_tag([fake], REPO, TAG, SHA)

    def test_500_rejected(self, tmp_path):
        state = {"api": {REFS_URL: {"__error__": {"code": 1, "status": 500,
                                                  "stderr": "gh: server error"}}}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="TAG_LOOKUP_FAILED"):
            rp.resolve_tag([fake], REPO, TAG, SHA)

    def test_lightweight_match(self, tmp_path):
        state = {"api": {REFS_URL: {"__body__": {"ref": f"refs/tags/{TAG}",
                                                "object": {"sha": SHA, "type": "commit"}},
                                   "__status__": 200}}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        assert rp.resolve_tag([fake], REPO, TAG, SHA)["action"] == "exists"

    def test_lightweight_elsewhere_rejected(self, tmp_path):
        state = {"api": {REFS_URL: {"__body__": {"ref": f"refs/tags/{TAG}",
                                                "object": {"sha": SHA2, "type": "commit"}},
                                   "__status__": 200}}}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="TAG_POINTS_ELSEWHERE"):
            rp.resolve_tag([fake], REPO, TAG, SHA)

    def test_annotated_match(self, tmp_path):
        tag_obj = "tagobjectsha00000000000000000000000000000001"
        state = {"api": {
            REFS_URL: {"__body__": {"ref": f"refs/tags/{TAG}",
                                    "object": {"sha": tag_obj, "type": "tag"}},
                       "__status__": 200},
            f"repos/{REPO}/git/tags/{tag_obj}": {"object": {"sha": SHA, "type": "commit"}},
        }}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        assert rp.resolve_tag([fake], REPO, TAG, SHA)["action"] == "exists"

    def test_annotated_deref_404_rejected(self, tmp_path):
        tag_obj = "tagobjectsha00000000000000000000000000000001"
        state = {"api": {
            REFS_URL: {"__body__": {"ref": f"refs/tags/{TAG}",
                                    "object": {"sha": tag_obj, "type": "tag"}},
                       "__status__": 200},
            f"repos/{REPO}/git/tags/{tag_obj}":
                {"__error__": {"code": 1, "status": 404, "stderr": "gh: Not Found (HTTP 404)"}},
        }}
        fake, env, _, _ = make_fake_gh(tmp_path, state)
        with pytest.raises(rp.PublicationError, match="TAG_PEEL_FAILED"):
            rp.resolve_tag([fake], REPO, TAG, SHA)

    def test_exact_ref_only(self, tmp_path):
        state = {"api": {REFS_URL: {"__error__": {"code": 1, "status": 404,
                                                  "stderr": "gh: Not Found (HTTP 404)"}}}}
        fake, env, _, log = make_fake_gh(tmp_path, state)
        assert rp.resolve_tag([fake], REPO, TAG, SHA)["action"] == "create"
        assert gh_log_entries(log)[0] == ["api", "--include", REFS_URL]


# ---------------------------------------------------------------- E. 发布 argv 形状与 repo 钉死


class TestReleaseArgv:
    def test_repo_pinned_before_double_dash(self):
        cmd = rp.build_release_argv(["gh"], REPO, TAG, SHA, "T", "notes.txt", ["a.exe", "b.exe"])
        assert cmd[:5] == ["gh", "release", "create", "--repo", REPO]
        assert cmd[5] == TAG
        dash = cmd.index("--")
        # --repo/--target/--title/--notes-file 必须在 -- 之前，否则被当资产文件名。
        for flag in ("--repo", "--target", "--title", "--notes-file"):
            assert cmd.index(flag) < dash, flag
        assert cmd[dash + 1:] == ["a.exe", "b.exe"]
        cmd2 = rp.build_release_argv(["/tmp/fake-gh"], REPO, TAG, SHA, "T", "n", [])
        assert cmd2[0] == "/tmp/fake-gh"

    def test_create_rejects_repo_conflict(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        write_receipt_for(pkg)
        source_file = tmp_path / "source.json"
        source_file.write_text(json.dumps(good_source()), encoding="utf-8")
        fake, env, _, _ = make_fake_gh(tmp_path, {"api": {}})
        done = subprocess.run(
            [sys.executable, str(HELPER), "--gh", fake, "create-release",
             "--repo", "Evil/Other", "--source", str(source_file),
             "--tag", TAG, "--package-dir", str(pkg), "--dry-run"],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(REPO_ROOT))
        assert done.returncode == 1
        assert "REPO_CONFLICT" in done.stderr

    def test_no_shell_in_helper_source(self):
        tree = ast.parse(HELPER.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert "shell" not in [k.arg for k in node.keywords], ast.dump(node)
        text = HELPER.read_text(encoding="utf-8")
        assert "os.system" not in text
        assert "os.popen" not in text

    def test_download_streams_without_memory(self):
        # 不变式 6：zip 不许整包进内存。stdout 直落临时文件，逐项按块拷贝。
        text = HELPER.read_text(encoding="utf-8")
        span = text.split("def _download_and_extract")[1].split("def stream_sha256")[0]
        assert "capture_output" not in span
        assert "stdout=sink" in span
        assert "copyfileobj" in span
        assert "archive_download_url" not in span


# ---------------------------------------------------------------- F. CLI 全流程（fake gh）


def run_cli(fake, env, *args):
    done = subprocess.run([sys.executable, str(HELPER), "--gh", fake, *args],
                          capture_output=True, text=True, timeout=120, env=env, cwd=str(REPO_ROOT))
    return done


class TestCliFlow:
    def full_state(self, pkg_files):
        return {
            "expect_repo": REPO,
            "expect_tag": TAG,
            "api": {
                **run_endpoints(good_run()),
                ARTIFACTS_URL: artifacts_page([artifact_entry()]),
                REFS_URL: {"__error__": {"code": 1, "status": 404,
                                         "stderr": "gh: Not Found (HTTP 404)"}},
            },
            "zip": pkg_files,
        }

    def test_full_flow_dry_run(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        write_receipt_for(pkg)
        fake, env, state_file, log = make_fake_gh(tmp_path, self.full_state(package_members(pkg)))
        source_file = tmp_path / "source.json"
        dest = tmp_path / "dest"

        done = run_cli(fake, env, "verify-run", "--repo", REPO, "--run-id", "35233429350",
                       "--tag", TAG, "--ref", "refs/heads/main", "--out", str(source_file))
        assert done.returncode == 0, done.stderr
        done = run_cli(fake, env, "fetch-artifact", "--repo", REPO,
                       "--source", str(source_file), "--dest", str(dest))
        assert done.returncode == 0, done.stderr
        done = run_cli(fake, env, "verify-package", "--dir", str(dest),
                       "--source", str(source_file), "--tag", TAG)
        assert done.returncode == 0, done.stderr
        done = run_cli(fake, env, "create-release", "--repo", REPO,
                       "--source", str(source_file), "--tag", TAG,
                       "--package-dir", str(dest), "--dry-run")
        assert done.returncode == 0, done.stderr
        payload = json.loads(done.stdout)
        argv = payload["argv"]
        assert payload["plan"] == {"action": "create", "tag": TAG, "target": SHA}
        assert argv[1:6] == ["release", "create", "--repo", REPO, TAG]
        assert argv.index("--target") < argv.index("--")
        names = sorted(Path(a).name for a in argv[argv.index("--") + 1:])
        assert names == sorted([SETUP_NAME, SETUP_NAME + ".sha256",
                                PORTABLE_NAME, PORTABLE_NAME + ".sha256",
                                "SHA256SUMS", "source-receipt.json"]), names

    def test_cli_real_create_hits_only_fake(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        write_receipt_for(pkg)
        fake, env, state_file, log = make_fake_gh(
            tmp_path, dict(self.full_state({}), expect_repo=REPO, expect_tag=TAG))
        source_file = tmp_path / "source.json"
        source_file.write_text(json.dumps(good_source()), encoding="utf-8")
        # GH_REPO 污染 + 异目录 cwd：写入仍必须走显式 --repo。
        env = dict(env, GH_REPO="Evil/Other")
        done = subprocess.run(
            [sys.executable, str(HELPER), "--gh", fake, "create-release",
             "--repo", REPO, "--source", str(source_file), "--tag", TAG,
             "--package-dir", str(pkg)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
        assert done.returncode == 0, done.stderr
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert len(saved["releases"]) == 1
        recorded = saved["releases"][0]
        assert recorded[0] == "--repo" and recorded[1] == REPO

    def test_cli_dry_run_still_verifies(self, tmp_path):
        pkg = tmp_path / "pkg"
        make_package(pkg)
        write_receipt_for(pkg)
        (pkg / "windows" / SETUP_NAME).write_bytes(b"tampered-after-receipt")
        fake, env, state_file, log = make_fake_gh(tmp_path, self.full_state({}))
        source_file = tmp_path / "source.json"
        source_file.write_text(json.dumps(good_source()), encoding="utf-8")
        done = run_cli(fake, env, "create-release", "--repo", REPO,
                       "--source", str(source_file), "--tag", TAG,
                       "--package-dir", str(pkg), "--dry-run")
        assert done.returncode == 1
        assert "PACKAGE_HASH_MISMATCH" in done.stderr

    def test_cli_injection_marker_never_executes(self, tmp_path):
        marker = tmp_path / "PWNED_CLI"
        fake, env, _, log = make_fake_gh(tmp_path, {"api": run_endpoints(good_run())})
        done = run_cli(fake, env, "verify-run", "--repo", REPO,
                       "--run-id", f"1$(touch {marker})", "--tag", TAG,
                       "--ref", "refs/heads/main", "--out", str(tmp_path / "s.json"))
        assert done.returncode == 1
        assert "BAD_RUN_ID" in done.stderr
        assert gh_log_entries(log) == []
        assert not marker.exists()

    def test_cli_missing_gh_is_clean_error(self, tmp_path):
        done = run_cli("/nonexistent/gh-binary", dict(os.environ),
                       "assert-ref", "--ref", "refs/heads/main")
        assert done.returncode == 0  # assert-ref 不调 gh
        done = run_cli("/nonexistent/gh-binary", dict(os.environ),
                       "verify-run", "--repo", REPO, "--run-id", "1",
                       "--tag", TAG, "--ref", "refs/heads/main",
                       "--out", str(tmp_path / "s.json"))
        assert done.returncode == 1
        assert "GH_EXEC_FAILED" in done.stderr

    def test_cli_missing_package_dir_rejected(self, tmp_path):
        fake, env, _, _ = make_fake_gh(tmp_path, {"api": {}})
        done = run_cli(fake, env, "create-release", "--repo", REPO,
                       "--source", "/nonexistent/s.json", "--tag", TAG)
        # --package-dir 必填：argparse 直接拒绝旁路。
        assert done.returncode == 2


# ---------------------------------------------------------------- G. workflow 按步骤守卫（R-208，非全文计数）


def desktop_steps():
    """把 desktop-windows.yml 按步骤切块：{name: block}。

    步骤条目起于 6 空格 `      - `；后续缩进 >= 8 或空行都属该步骤；
    缩进更小即步骤结束。只做形状断言，不引入 YAML 依赖。
    """
    blocks, name, current = {}, None, []
    for line in DESKTOP_YML.read_text(encoding="utf-8").splitlines():
        if line.startswith("      - "):
            if name is not None:
                blocks[name] = "\n".join(current)
            rest = line[len("      - "):]
            if rest.startswith("name: "):
                name, current = rest[len("name: "):].strip(), [line]
            else:
                name, current = None, []
        elif name is not None:
            if line.strip() == "" or len(line) - len(line.lstrip()) >= 8:
                current.append(line)
            else:
                blocks[name] = "\n".join(current)
                name, current = None, []
    if name is not None:
        blocks[name] = "\n".join(current)
    return blocks


def has_step_key(block, key):
    return any(line.strip() == key or line.strip().startswith(key + ":") or line.strip().startswith(key + " ")
               for line in block.splitlines() if len(line) - len(line.lstrip()) == 8
               and not line.strip().startswith("-"))


class TestWorkflowShape:
    def test_release_run_blocks_have_no_interpolation(self):
        for block in _run_blocks(RELEASE_YML):
            assert "${{" not in block, block

    def test_release_uses_env_for_inputs(self):
        # 输入只许出现在 env: 映射里；run: 脚本块里只许 $VAR，不许 inputs./github.。
        for block in _run_blocks(RELEASE_YML):
            assert "inputs." not in block, block
            assert "github." not in block, block

    def test_release_wires_gh_token(self):
        # gh 不读 checkout 凭据：没有 GH_TOKEN，第一步 API 就认证失败。
        text = RELEASE_YML.read_text(encoding="utf-8")
        assert "GH_TOKEN" in text and "${{ github.token }}" in text

    def test_gui_smoke_is_blocking_gate(self):
        steps = desktop_steps()
        smoke = steps["Windows GUI smoke（阻塞门）"]
        assert "continue-on-error" not in smoke
        assert "smoke-windows.mjs" in smoke
        assert not has_step_key(smoke, "if"), "smoke 必须是默认 success 路径"

    def test_wsl_spike_stays_diagnostic(self):
        steps = desktop_steps()
        spike = steps["WSL spike（诊断，不判失败）"]
        assert "continue-on-error: true" in spike

    def test_receipt_and_upload_on_success_path(self):
        steps = desktop_steps()
        for name in ("生成来源清单", "上传安装包（两个 exe + release 清单 + sha256 + 来源清单）"):
            block = steps[name]
            assert "continue-on-error" not in block, name
            assert not has_step_key(block, "if"), f"{name} 必须是默认 success 路径"

    def test_smoke_evidence_still_always(self):
        steps = desktop_steps()
        evidence = steps["上传 smoke 证据（报告 / 日志 / 失败原因）"]
        assert "if: always()" in evidence

    def test_desktop_paths_cover_release_chain(self):
        text = DESKTOP_YML.read_text(encoding="utf-8")
        for entry in ("scripts/release_publication.py",
                      "tests/test_release_publication.py",
                      ".github/workflows/release.yml"):
            assert text.count(entry) >= 2, entry  # push 与 pull_request 两份列表


def _run_blocks(path):
    """release.yml 里所有 run: 脚本的行。输入只许走 env:，run: 里不许出现插值。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "run: |":
            indent = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            current = []
            while i < len(lines):
                line = lines[i]
                if line.strip() == "" or len(line) - len(line.lstrip()) > indent:
                    current.append(line)
                    i += 1
                else:
                    break
            blocks.append("\n".join(current))
        else:
            if stripped.startswith("run:") and stripped != "run: |":
                blocks.append(lines[i])
            i += 1
    return blocks
