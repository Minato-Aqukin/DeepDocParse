"""发布链最小闭环的集中 helper（T-007）。

只做四件事，全部用标准库：

1. 来源核验（``verify-run``）：用 ``gh api`` 读指定 run 的元数据，要求
   同仓库 / 非 fork / 固定 workflow 路径 / main 分支 / push 或
   workflow_dispatch / completed+success / pull_requests 为空 / head_sha
   合法，并把 ``apps/desktop/package.json`` 在该 commit 的版本作为外部
   版本锚点读回。缺字段、API 错误一律 fail，不下载不发布。
2. 构件获取（``fetch-artifact``）：完整分页后收集全部同名候选，逐个校验
   expired 是严格 bool（缺/null/字符串/数字一律 fail），再区分 live 与
   expired 并判断唯一；用校验过的 artifact ID 构造同仓固定 API 路径下载
   zip 到临时文件再逐项流式解包（大文件不进内存）；artifact 的 workflow_run
   （run id/仓库 ID/分支/SHA）与运行 API 逐项对照，不只看 SHA。artifact
   条目没有 run_attempt 字段，attempt 对照放在第 3 步用来源清单比运行
   API。含混 / 过期 / 缺失 / 对不上 SHA 一律拒绝。
3. 安装包校验（``verify-package``）：唯一 setup / 唯一 portable / 唯一
   SHA256SUMS / 两份 sidecar；清单必须精确覆盖这两个文件各一次；三处
   （清单、sidecar、来源清单）哈希与实际字节流一致；来源清单与 API 元
   数据逐字段对照；tag、来源锚点版本、来源清单版本三者一致。
4. tag 解析与发布（``resolve-tag`` / ``create-release``）：精确 ref 查询
   明确 404 才表示新 tag；已有 tag 必须剥离（轻量与 annotated，解引用
   404/403/500 一律拒绝）后等于来源 SHA；新 tag 显式 ``--target`` 来源
   SHA；发布集合含 setup/portable/双 sidecar/完整 SHA256SUMS/来源清单，
   与清单覆盖严格对应，不留悬空引用。

设计约束（对应验收 A–F）：

- 所有 ``gh`` 调用都是参数列表（``subprocess.run([...])``），绝不用
  ``shell=True``，也不把输入拼进 shell 字符串；workflow 里 dispatch 输入
  只走 ``env:`` 进 helper 的 argv/环境。
- checksum 只是完整性校验，**不声称签名，也不防同信任域作恶**（能改包的
  人同样能重算清单）。这道门防的是拿错包、旧包、PR/fork 包、失败包和
  手误 tag，不是防构建机被攻破。
- 旧构件没有来源清单（``source-receipt.json``）时明确拒绝并提示重建，
  不提供绕过开关。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# 信源常量：写死，不接受输入改写。
SOURCE_WORKFLOW_PATH = ".github/workflows/desktop-windows.yml"
SOURCE_EVENTS = ("push", "workflow_dispatch")
SOURCE_BRANCH = "main"
ARTIFACT_NAME = "windows-installers"
RECEIPT_NAME = "source-receipt.json"
RECEIPT_SCHEMA = "ddp-source-receipt/1"
PACKAGE_JSON = "apps/desktop/package.json"

RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SETUP_GLOB = "DeepDocParse-*-setup.exe"
PORTABLE_GLOB = "DeepDocParse-*-portable.exe"
# 真实命名锚点（packaging/windows/electron-builder.yml artifactName）：
# DeepDocParse-<version>-win-x64-setup.exe / -portable.exe。
# 文件名里的 version 必须等于来源版本，不接受"哈希自洽但名字是别的版本"。
PACKAGE_WINDOWS_SUBDIR = "windows"
CHUNK = 1 << 20


def expected_setup_name(version: str) -> str:
    return f"DeepDocParse-{version}-win-x64-setup.exe"


def expected_portable_name(version: str) -> str:
    return f"DeepDocParse-{version}-win-x64-portable.exe"


def strict_int(value: object, what: str) -> int:
    """JSON 整数且必须是真 int：bool 是 int 子类，True 不得充当 1。"""
    if type(value) is not int:
        _fail("BAD_FIELD_TYPE", f"{what} must be an integer, got {value!r}")
    return value


class PublicationError(Exception):
    """失败即整步红。``code`` 是机器可 grep 的稳定前缀。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> "None":
    raise PublicationError(code, message)


def validate_run_id(text: str) -> int:
    """run_id 严格正整数：拒绝空、`$(...)`、分号、前导零、超长等一切非数字形态。"""
    if not RUN_ID_RE.match(text or ""):
        _fail("BAD_RUN_ID", f"run_id must be a positive integer, got {text!r}")
    return int(text)


def validate_tag(text: str) -> str:
    """tag 安全语法：只许 vX.Y.Z，不许前导 dash、空格、路径分隔与 shell 元字符。"""
    if not TAG_RE.match(text or ""):
        _fail("BAD_TAG", f"tag must match vX.Y.Z, got {text!r}")
    return text


def validate_sha(text: str, what: str = "sha") -> str:
    if not SHA_RE.match(text or ""):
        _fail("BAD_SHA", f"{what} must be 40 lowercase hex, got {text!r}")
    return text


def assert_trusted_ref(ref: str) -> str:
    """发布 workflow 自身必须从可信 main 执行，拒绝其他 ref（含 PR 合并 ref）。"""
    if ref != "refs/heads/main":
        _fail("UNTRUSTED_REF", f"release must run from refs/heads/main, got {ref!r}")
    return ref


def run_gh(gh: list[str], repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """gh 调用：参数列表直传，无 shell 解释。失败只返回码，不抛，由调用方定语义。"""
    try:
        return subprocess.run(
            [*gh, *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("GH_EXEC_FAILED", f"cannot execute {' '.join([*gh, *args][:3])}: {exc}")


def gh_api(gh: list[str], repo: str, api_path: str) -> object:
    done = run_gh(gh, repo, "api", api_path)
    if done.returncode != 0:
        _fail("GH_API_FAILED", f"gh api {api_path} exited {done.returncode}: {done.stderr.strip()[:300]}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        _fail("GH_API_BAD_JSON", f"gh api {api_path} did not return JSON")


def gh_api_include(gh: list[str], api_path: str) -> tuple[int, str]:
    """带响应头的 gh api 调用，返回 (http_status, body)。

    执行失败（起不来/超时）返回 status 0；调用方按状态码分支，
    绝不看 stderr 子串猜状态（'trace id 404cafe' 曾被误判成 404）。
    """
    try:
        done = subprocess.run([*gh, "api", "--include", api_path],
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("GH_EXEC_FAILED", f"cannot execute gh api: {exc}")
        raise AssertionError("unreachable")
    head, sep, body = done.stdout.partition("\r\n\r\n")
    if not sep:
        head, sep, body = done.stdout.partition("\n\n")
    first = head.splitlines()[0] if head.strip() else ""
    match = re.match(r"^HTTP/\S+\s+(\d{3})\b", first)
    if not match:
        return 0, body
    return int(match.group(1)), body


def gh_api_json(gh: list[str], repo: str, api_path: str) -> dict:
    data = gh_api(gh, repo, api_path)
    if not isinstance(data, dict):
        _fail("GH_API_BAD_SHAPE", f"gh api {api_path} returned non-object JSON")
    return data


def verify_run(gh: list[str], repo: str, run_id_text: str, ref: str) -> dict:
    """验收 A：输入校验 + 发布 ref + GitHub API 来源核验 + 外部版本锚点。"""
    run_id = validate_run_id(run_id_text)
    assert_trusted_ref(ref)
    if not repo or "/" not in repo:
        _fail("BAD_REPO", f"repo must be owner/name, got {repo!r}")

    run = gh_api_json(gh, repo, f"repos/{repo}/actions/runs/{run_id}")

    def need(key: str) -> object:
        value = run.get(key, None)
        if value is None or value == "":
            _fail("SOURCE_FIELD_MISSING", f"run {run_id} has no usable {key!r}")
        return value

    # 响应必须就是请求的那次 run：id 对不上（缓存错乱/串 run）直接拒绝。
    if strict_int(run.get("id"), "run id") != run_id:
        _fail("SOURCE_ID_MISMATCH", f"run API returned id {run.get('id')!r} for requested {run_id}")

    repository = run.get("repository")
    head_repository = run.get("head_repository")
    if not isinstance(repository, dict) or not isinstance(head_repository, dict):
        _fail("SOURCE_FIELD_MISSING", f"run {run_id} has no usable repository objects")
    if repository.get("full_name") != repo:
        _fail("SOURCE_REPO_MISMATCH", f"run {run_id} belongs to {repository.get('full_name')!r}")
    if head_repository.get("full_name") != repo:
        _fail("SOURCE_FORK", f"run {run_id} head repo is {head_repository.get('full_name')!r}")
    # 数字 ID 一并记下：构件侧 workflow_run 的 repository_id /
    # head_repository_id 要与这里逐项对照，不只比名字。
    repo_id = strict_int(repository.get("id"), "repository id")
    head_repo_id = strict_int(head_repository.get("id"), "head repository id")
    def need_str(key: str) -> str:
        value = need(key)
        if not isinstance(value, str):
            _fail("BAD_FIELD_TYPE", f"run {run_id} {key!r} is not a string: {value!r}")
        return value

    # 精确相等：官方示例可能带 @main 后缀，不要宽泛 endswith 绕过。
    if need_str("path") != SOURCE_WORKFLOW_PATH:
        _fail("SOURCE_WORKFLOW_MISMATCH", f"run {run_id} path is {run.get('path')!r}")
    if need_str("head_branch") != SOURCE_BRANCH:
        _fail("SOURCE_BRANCH_MISMATCH", f"run {run_id} head_branch is {run.get('head_branch')!r}")
    if need_str("event") not in SOURCE_EVENTS:
        _fail("SOURCE_EVENT_REJECTED", f"run {run_id} event is {run.get('event')!r}")
    if need_str("status") != "completed":
        _fail("SOURCE_NOT_COMPLETED", f"run {run_id} status is {run.get('status')!r}")
    if need_str("conclusion") != "success":
        _fail("SOURCE_NOT_SUCCESS", f"run {run_id} conclusion is {run.get('conclusion')!r}")
    if run.get("pull_requests") != []:
        # 键缺失（None）与非空列表都拒绝：缺字段不得当作"无 PR"放行。
        _fail("SOURCE_HAS_PRS", f"run {run_id} pull_requests is {run.get('pull_requests')!r}")
    head_sha = validate_sha(str(need("head_sha")), "head_sha")
    attempt_raw = strict_int(run.get("run_attempt"), "run_attempt")
    if attempt_raw < 1:
        _fail("SOURCE_BAD_ATTEMPT", f"run {run_id} run_attempt is {attempt_raw!r}")

    version = source_version_at(gh, repo, head_sha)
    return {
        "repo": repo,
        "run_id": run_id,
        "run_attempt": attempt_raw,
        "head_sha": head_sha,
        "version": version,
        "repo_id": repo_id,
        "head_repo_id": head_repo_id,
    }


def source_version_at(gh: list[str], repo: str, sha: str) -> str:
    """外部版本锚点：来源 commit 里 package.json 的版本，不是用户输入。

    锚点读不到（404/403/500/坏形状）一律视为锚点不可用而 fail，
    不回退到发布者当前 HEAD，更不静默放行。
    """
    try:
        blob = gh_api_json(gh, repo, f"repos/{repo}/contents/{PACKAGE_JSON}?ref={sha}")
    except PublicationError as exc:
        _fail("VERSION_ANCHOR_UNAVAILABLE", f"cannot read {PACKAGE_JSON} at {sha}: {exc}")
    if blob.get("encoding") != "base64" or not blob.get("content"):
        _fail("VERSION_ANCHOR_UNAVAILABLE", f"cannot read {PACKAGE_JSON} at {sha}")
    try:
        payload = json.loads(base64.b64decode(blob["content"]))
    except (ValueError, json.JSONDecodeError):
        _fail("VERSION_ANCHOR_UNREADABLE", f"{PACKAGE_JSON} at {sha} is not JSON")
    version = payload.get("version", "")
    if not VERSION_RE.match(str(version or "")):
        _fail("VERSION_ANCHOR_BAD", f"{PACKAGE_JSON} at {sha} version is {version!r}")
    return str(version)


def check_tag_matches_version(tag: str, version: str) -> str:
    tag = validate_tag(tag)
    if tag != f"v{version}":
        _fail("TAG_VERSION_MISMATCH", f"tag {tag} does not match source version {version}")
    return tag


def fetch_artifact(gh: list[str], repo: str, source: dict, dest: str) -> dict:
    """验收 B 前半：完整分页后，同名候选先验 expired 严格 bool，再判唯一下载。

    unknown（缺/null/字符串/数字）绝不忽略：{live A, unknown B} 必须整步红，
    不能从过滤结果反推原集合唯一。
    """
    collected: list[dict] = []
    total: int | None = None
    for page in range(1, 21):
        data = gh_api_json(gh, repo, f"repos/{repo}/actions/runs/{source['run_id']}/artifacts?per_page=100&page={page}")
        items = data.get("artifacts")
        if not isinstance(items, list):
            _fail("ARTIFACT_LIST_BAD", "artifacts API returned no list")
        page_total = data.get("total_count")
        if type(page_total) is not int or page_total < 0:
            _fail("ARTIFACT_LIST_BAD", "artifacts API returned no total_count")
        if total is None:
            total = page_total
        elif page_total != total:
            _fail("ARTIFACT_LIST_CHANGED", "artifacts total_count changed mid-pagination")
        if not items and len(collected) < total:
            _fail("ARTIFACT_LIST_INCOMPLETE", f"page {page} empty with {len(collected)}/{total} collected")
        collected.extend(a for a in items if isinstance(a, dict))
        if len(collected) >= total:
            break
    else:
        _fail("ARTIFACT_LIST_TRUNCATED", "artifacts pagination did not converge")
    if len(collected) > total:
        _fail("ARTIFACT_LIST_CHANGED", "artifacts grew mid-pagination")
    seen_ids: set[int] = set()
    for entry in collected:
        entry_id = entry.get("id")
        if type(entry_id) is not int:
            _fail("ARTIFACT_BAD_ID", f"artifact id is {entry_id!r}")
        if entry_id in seen_ids:
            _fail("ARTIFACT_LIST_DUPLICATE", f"artifact id {entry_id} listed twice")
        seen_ids.add(entry_id)
    # 同名候选集先验：把全部 windows-installers 收集出来，逐个校验 expired
    # 是严格 bool（缺/null/字符串/数字一律 fail），之后才能区分 False-live
    # 与 True-expired 并判断唯一。从过滤后的 live 结果反推原集合唯一是错的：
    # {live A, unknown B} 会被静默压成 [A] 下错下载。unknown 绝不忽略。
    same_name = [a for a in collected if a.get("name") == ARTIFACT_NAME]
    for candidate in same_name:
        state = candidate.get("expired")
        if type(state) is not bool:
            _fail("ARTIFACT_EXPIRED_UNKNOWN",
                  f"artifact {candidate.get('id')} expired is {state!r}, refusing to guess")
    live = [a for a in same_name if a["expired"] is False]
    if not live and not same_name:
        _fail("ARTIFACT_MISSING", f"no {ARTIFACT_NAME!r} artifact on run {source['run_id']}")
    if not live:
        _fail("ARTIFACT_MISSING", f"no live {ARTIFACT_NAME!r} artifact on run {source['run_id']}")
    if len(live) > 1:
        _fail(
            "ARTIFACT_AMBIGUOUS",
            f"{len(live)} live {ARTIFACT_NAME!r} artifacts on run {source['run_id']}; "
            "reruns leave several, refuse rather than guess",
        )
    artifact = live[0]
    artifact_id = artifact.get("id")
    if artifact_id is None or artifact_id < 1:
        _fail("ARTIFACT_BAD_ID", f"artifact id is {artifact_id!r}")
    # 构件元信息的 workflow_run 与运行 API 逐项对照，不只看 SHA：
    # run id、同仓数字 ID、分支、SHA 全都得对上。
    flow = artifact.get("workflow_run")
    if not isinstance(flow, dict):
        _fail("ARTIFACT_FIELD_MISSING", "artifact has no workflow_run object")

    def flow_int(key: str, what: str) -> int:
        # 精确类型：True == 1 但 type 不同，bool 不得充数。
        value = flow.get(key)
        if type(value) is not int:
            _fail("ARTIFACT_FIELD_MISSING", f"artifact workflow_run {what} is {value!r}")
        return value

    if flow_int("id", "run id") != source["run_id"]:
        _fail("ARTIFACT_RUN_MISMATCH", "artifact workflow_run id differs from run id")
    if flow_int("repository_id", "repository id") != source["repo_id"]:
        _fail("ARTIFACT_REPO_MISMATCH", "artifact repository_id differs from run repository")
    if flow_int("head_repository_id", "head repository id") != source["head_repo_id"]:
        _fail("ARTIFACT_REPO_MISMATCH", "artifact head_repository_id differs from run head repository")
    if flow.get("head_branch") != SOURCE_BRANCH:
        _fail("ARTIFACT_BRANCH_MISMATCH", f"artifact head_branch is {flow.get('head_branch')!r}")
    if flow.get("head_sha") != source["head_sha"]:
        _fail("ARTIFACT_SHA_MISMATCH", "artifact workflow_run head_sha differs from run head_sha")
    dest_path = Path(dest)
    _download_and_extract(gh, repo, artifact_id, dest_path)
    return {"artifact_id": artifact_id, "digest": artifact.get("digest")}


def _download_and_extract(gh: list[str], repo: str, artifact_id: int, dest: Path) -> None:
    """按 ID 构造同仓固定 API 路径下载 zip，落临时文件后逐项流式解包。

    不从 artifact 元信息里取任意下载 URL（那是带 token 的外链形状，不应被
    信任为下载地址）；不把整包读进内存（AGENTS.md 不变式 6：大文件不得
    完整进入应用进程内存），stdout 直接重定向到系统临时文件，解包时每项
    按块拷贝。

    写前安全（R-205，Reviewer 真实反例：dest 预置软链导致目录外文件被改）：
    先拒绝不干净的 dest（自身/祖先是软链、已非空），再完整预检全部成员
    （类型/重复/冲突路径/边界），最后逐项 O_NOFOLLOW|O_EXCL 写入，
    不跟随任何已有软链，不 unlink 任何别人的文件。
    """
    _require_clean_dest(dest)
    api_path = f"repos/{repo}/actions/artifacts/{artifact_id}/zip"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with tmp_path.open("wb") as sink:
            try:
                done = subprocess.run([*gh, "api", api_path], stdout=sink,
                                      stderr=subprocess.PIPE, timeout=600, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _fail("ARTIFACT_DOWNLOAD_FAILED", f"artifact download failed: {exc}")
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()[:300]
            _fail("ARTIFACT_DOWNLOAD_FAILED", f"artifact download exited {done.returncode}: {detail}")
        if tmp_path.stat().st_size < 2:
            _fail("ARTIFACT_DOWNLOAD_EMPTY", "artifact download is empty")
        with tmp_path.open("rb") as head:
            if head.read(2) != b"PK":
                _fail("ARTIFACT_DOWNLOAD_BAD", "artifact download is not a zip archive")
        try:
            with zipfile.ZipFile(tmp_path) as archive:
                members = _prescan_members(archive)
                for info, kind in members:
                    _extract_member(archive, info, kind, dest)
        except zipfile.BadZipFile as exc:
            _fail("ARTIFACT_DOWNLOAD_BAD", f"artifact zip unreadable: {exc}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    if not any(dest.iterdir()):
        _fail("ARTIFACT_DOWNLOAD_EMPTY", "artifact zip extracted nothing")


def _require_clean_dest(dest: Path) -> None:
    """dest 自身与全部祖先不得是软链；已存在非空目录拒绝而非覆盖。"""
    for node in (dest, *dest.parents):
        if os.path.islink(node):
            _fail("DEST_NOT_CLEAN", f"refusing to write through symlink: {node}")
    if dest.exists() and not dest.is_dir():
        _fail("DEST_NOT_CLEAN", f"refusing non-directory dest: {dest}")
    if dest.is_dir() and any(dest.iterdir()):
        _fail("DEST_NOT_CLEAN", f"refusing non-empty dest, clean it first: {dest}")
    dest.mkdir(parents=True, exist_ok=True)


def _prescan_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """解包前完整预检：先看完全部成员再写第一个字节。

    拒绝：绝对路径、Windows 盘符/反斜杠、`..` 穿越、symlink 模式位、
    特殊类型（fifo/socket/device）、重复规范路径、文件/目录冲突。
    """
    import stat as statmod

    planned: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    file_paths: set[str] = set()
    dir_paths: set[str] = {""}
    for info in archive.infolist():
        name = info.filename
        if (not name or name.startswith("/") or name.startswith("\\")
                or "\\" in name or re.match(r"^[A-Za-z]:", name)
                or ".." in Path(name).parts):
            _fail("ARTIFACT_ZIP_SLIP", f"unsafe member {name!r} in artifact zip")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == statmod.S_IFLNK:
            _fail("ARTIFACT_ZIP_SYMLINK", f"symlink member {name!r} in artifact zip")
        if mode not in (0, statmod.S_IFREG, statmod.S_IFDIR):
            _fail("ARTIFACT_ZIP_SPECIAL", f"special member {name!r} (mode {oct(mode)}) in artifact zip")
        norm = name.rstrip("/")
        is_dir = info.is_dir() or name.endswith("/")
        if norm in seen:
            _fail("ARTIFACT_ZIP_DUPLICATE", f"duplicate member {name!r} in artifact zip")
        seen.add(norm)
        parts = norm.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            if prefix in file_paths:
                _fail("ARTIFACT_ZIP_CONFLICT", f"member {name!r} conflicts with file {prefix!r}")
        if is_dir:
            if norm in file_paths:
                _fail("ARTIFACT_ZIP_CONFLICT", f"member {name!r} conflicts with same-name file")
            dir_paths.add(norm)
        else:
            if norm in dir_paths:
                _fail("ARTIFACT_ZIP_CONFLICT", f"member {name!r} conflicts with a directory")
            file_paths.add(norm)
        planned.append((info, "dir" if is_dir else "file"))
    return planned


def _extract_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, kind: str, dest: Path) -> None:
    """逐项写入：沿途已有软链即停；文件 O_NOFOLLOW|O_EXCL，不跟随不覆盖。"""
    target = dest / info.filename
    node = dest
    for part in target.relative_to(dest).parts[:-1]:
        node = node / part
        if os.path.islink(node):
            _fail("DEST_NOT_CLEAN", f"refusing to write through symlink: {node}")
        node.mkdir(exist_ok=True)
        if not node.is_dir() or os.path.islink(node):
            _fail("DEST_NOT_CLEAN", f"refusing non-directory path: {node}")
    if kind == "dir":
        if os.path.lexists(target) and (os.path.islink(target) or not target.is_dir()):
            _fail("DEST_NOT_CLEAN", f"refusing non-directory member path: {target}")
        target.mkdir(exist_ok=True)
        return
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        _fail("DEST_NOT_CLEAN", f"refusing existing member path: {target}")
    except OSError as exc:
        _fail("DEST_NOT_CLEAN", f"cannot create member path {target}: {exc}")
    try:
        with archive.open(info) as src, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out, CHUNK)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise


def stream_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _unique_file(directory: Path, pattern: str, what: str) -> Path:
    found = sorted(
        p for p in directory.glob(pattern)
        if p.is_file() and not p.is_symlink()
    )
    if not found:
        _fail("PACKAGE_FILE_MISSING", f"no {what} matching {pattern} in {directory}")
    if len(found) > 1:
        _fail("PACKAGE_FILE_AMBIGUOUS", f"{len(found)} {what} candidates: {[p.name for p in found]}")
    return found[0]


STRAY_PATTERNS = (SETUP_GLOB, PORTABLE_GLOB, "SHA256SUMS", RECEIPT_NAME, "DeepDocParse-*.exe.sha256")


def locate_package(path: Path) -> Path:
    """真实构件布局锚定：exe/清单/receipt 住在固定 ``windows/`` 子目录。

    接受构件根（含 ``windows/`` 子目录）或 ``windows/`` 本目录（构建侧直接
    传它）；其他同名候选出现在根或别处一律拒绝，分不清该信哪份就整步红。
    历史 ``release.json`` 等归档名字不同，不受影响，照常保留。
    """
    if path.is_symlink():
        _fail("PACKAGE_LAYOUT", f"refusing symlinked package path: {path}")
    if (path / PACKAGE_WINDOWS_SUBDIR).is_dir() and not (path / PACKAGE_WINDOWS_SUBDIR).is_symlink():
        root, sub = path, path / PACKAGE_WINDOWS_SUBDIR
    elif path.name == PACKAGE_WINDOWS_SUBDIR and path.is_dir():
        root, sub = path.parent, path
    else:
        _fail("PACKAGE_LAYOUT", f"no real {PACKAGE_WINDOWS_SUBDIR}/ subdir under {path}")
        raise AssertionError("unreachable")
    strays: list[str] = []
    for pattern in STRAY_PATTERNS:
        for candidate in root.rglob(pattern):
            if candidate.is_file() and not candidate.is_symlink() and candidate.parent != sub:
                strays.append(str(candidate.relative_to(root)))
    if strays:
        _fail("PACKAGE_STRAY_CANDIDATE", f"same-name candidates outside {PACKAGE_WINDOWS_SUBDIR}/: {sorted(strays)}")
    return sub


def check_filenames(version: str, setup: Path, portable: Path, code: str) -> None:
    """文件名里的 version/platform 必须等于来源版本（R-207）。

    命名锚点是既有 electron-builder.yml artifactName：
    ``DeepDocParse-<version>-win-x64-setup.exe``，不是新发明的后缀。
    哈希自洽但名字是别的版本（如 9.9.9 文件配 0.1.0 来源）必须拒绝。
    """
    if setup.name != expected_setup_name(version):
        _fail(code, f"setup name {setup.name!r} does not match source version {version}")
    if portable.name != expected_portable_name(version):
        _fail(code, f"portable name {portable.name!r} does not match source version {version}")


def _parse_hash_line(line: str, source: str) -> tuple[str, str]:
    """`<hex64><空白><文件名>`；文件名不许带目录、分隔符与穿越。"""
    parts = line.split()
    if len(parts) != 2:
        _fail("HASH_LINE_MALFORMED", f"{source}: malformed line {line!r}")
    digest, name = parts
    if not HEX64_RE.match(digest):
        _fail("HASH_LINE_BAD_DIGEST", f"{source}: bad digest in {line!r}")
    if "/" in name or "\\" in name or name in (".", "..") or ".." in Path(name).parts:
        _fail("HASH_PATH_TRAVERSAL", f"{source}: unsafe file name {name!r}")
    return digest, name


def write_receipt(
    windows_dir: str,
    package_json: str,
    repo: str,
    run_id_text: str,
    attempt_text: str,
    head_sha: str,
    out: str,
) -> dict:
    """构建侧（可信 Windows job，阻塞 smoke 成功后）：生成来源清单。

    版本取自本次检出里的 package.json（可信构建时的版本），文件名与字节流
    哈希现场计算。任何一项对不上就非零退出，后续上传步骤不会执行。
    """
    run_id = validate_run_id(run_id_text)
    head_sha = validate_sha(head_sha, "head_sha")
    try:
        attempt = int(attempt_text or "")
    except ValueError:
        _fail("BAD_ATTEMPT", f"run_attempt must be an integer, got {attempt_text!r}")
    if attempt < 1:
        _fail("BAD_ATTEMPT", f"run_attempt must be >= 1, got {attempt_text!r}")
    if not repo or "/" not in repo:
        _fail("BAD_REPO", f"repo must be owner/name, got {repo!r}")

    try:
        version = json.loads(Path(package_json).read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError) as exc:
        _fail("RECEIPT_NO_VERSION", f"cannot read version from {package_json}: {exc}")
    if not VERSION_RE.match(str(version or "")):
        _fail("RECEIPT_BAD_VERSION", f"version in {package_json} is {version!r}")

    directory = locate_package(Path(windows_dir))
    setup = _unique_file(directory, SETUP_GLOB, "setup installer")
    portable = _unique_file(directory, PORTABLE_GLOB, "portable exe")
    check_filenames(version, setup, portable, "RECEIPT_FILENAME_MISMATCH")
    sums = _unique_file(directory, "SHA256SUMS", "checksum list")
    setup_sidecar = directory / (setup.name + ".sha256")
    portable_sidecar = directory / (portable.name + ".sha256")
    for sidecar in (setup_sidecar, portable_sidecar):
        if not sidecar.is_file() or sidecar.is_symlink():
            _fail("PACKAGE_FILE_MISSING", f"missing sidecar {sidecar.name}")

    setup_hash, setup_size = stream_sha256(setup)
    portable_hash, portable_size = stream_sha256(portable)
    sums_hash, _ = stream_sha256(sums)
    # 清单必须精确覆盖这两个文件各一次：复用发布侧同一解析器，行为一致。
    covered = parse_sums(sums, {setup.name, portable.name})
    if covered[setup.name] != setup_hash:
        _fail("RECEIPT_SUMS_MISMATCH", f"SHA256SUMS entry for {setup.name} differs from bytes")
    if covered[portable.name] != portable_hash:
        _fail("RECEIPT_SUMS_MISMATCH", f"SHA256SUMS entry for {portable.name} differs from bytes")

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "repo": repo,
        "workflow_path": SOURCE_WORKFLOW_PATH,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": head_sha,
        "version": version,
        "setup": {"name": setup.name, "sha256": setup_hash, "size": setup_size},
        "portable": {"name": portable.name, "sha256": portable_hash, "size": portable_size},
        "sums_sha256": sums_hash,
    }
    out_path = Path(out)
    out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def parse_sums(sums_path: Path, expected_names: set[str]) -> dict[str, str]:
    """SHA256SUMS 必须精确覆盖 expected 文件各一次：缺失、多余、重复、坏哈希全拒。"""
    try:
        text = sums_path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail("PACKAGE_FILE_MISSING", f"cannot read {sums_path}: {exc}")
    entries: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        digest, name = _parse_hash_line(line, sums_path.name)
        if name in entries:
            _fail("SUMS_DUPLICATE", f"{sums_path.name} lists {name!r} twice")
        entries[name] = digest
    if set(entries) != expected_names:
        missing = sorted(expected_names - set(entries))
        extra = sorted(set(entries) - expected_names)
        _fail("SUMS_COVERAGE", f"{sums_path.name} must list exactly {sorted(expected_names)}; missing={missing} extra={extra}")
    return entries


def read_sidecar(sidecar: Path, expect_name: str) -> str:
    try:
        line = sidecar.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _fail("PACKAGE_FILE_MISSING", f"cannot read {sidecar.name}: {exc}")
    digest, name = _parse_hash_line(line, sidecar.name)
    if name != expect_name:
        _fail("SIDECAR_NAME_MISMATCH", f"{sidecar.name} names {name!r}, expected {expect_name!r}")
    return digest


def verify_package(directory: str, source: dict, tag: str) -> dict:
    """验收 C：文件唯一性、清单精确覆盖、三处哈希与字节流一致、来源对照、版本三方一致。"""
    sub = locate_package(Path(directory))
    setup = _unique_file(sub, SETUP_GLOB, "setup installer")
    portable = _unique_file(sub, PORTABLE_GLOB, "portable exe")
    check_filenames(source["version"], setup, portable, "PACKAGE_FILENAME_MISMATCH")
    sums = _unique_file(sub, "SHA256SUMS", "checksum list")
    setup_sidecar = sub / (setup.name + ".sha256")
    portable_sidecar = sub / (portable.name + ".sha256")
    receipt_path = sub / RECEIPT_NAME
    for extra in (setup_sidecar, portable_sidecar, receipt_path):
        if not extra.is_file() or extra.is_symlink():
            _fail("PACKAGE_FILE_MISSING", f"missing {extra.name} in {directory}")

    setup_hash, _ = stream_sha256(setup)
    portable_hash, _ = stream_sha256(portable)
    covered = parse_sums(sums, {setup.name, portable.name})
    if covered[setup.name] != setup_hash:
        _fail("PACKAGE_HASH_MISMATCH", f"{setup.name} bytes differ from SHA256SUMS")
    if covered[portable.name] != portable_hash:
        _fail("PACKAGE_HASH_MISMATCH", f"{portable.name} bytes differ from SHA256SUMS")
    if read_sidecar(setup_sidecar, setup.name) != setup_hash:
        _fail("PACKAGE_HASH_MISMATCH", f"{setup_sidecar.name} differs from bytes")
    if read_sidecar(portable_sidecar, portable.name) != portable_hash:
        _fail("PACKAGE_HASH_MISMATCH", f"{portable_sidecar.name} differs from bytes")

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("RECEIPT_UNREADABLE", f"{RECEIPT_NAME}: {exc}")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        _fail("RECEIPT_SCHEMA", f"{RECEIPT_NAME} schema is {receipt.get('schema')!r}")
    for key in ("repo", "run_id", "run_attempt", "head_sha", "version"):
        if receipt.get(key) != source[key]:
            _fail("RECEIPT_SOURCE_MISMATCH", f"{RECEIPT_NAME} {key}={receipt.get(key)!r} != source {source[key]!r}")
    if receipt.get("workflow_path") != SOURCE_WORKFLOW_PATH:
        _fail("RECEIPT_SOURCE_MISMATCH", f"{RECEIPT_NAME} workflow_path={receipt.get('workflow_path')!r}")
    for key, path, actual in (("setup", setup, setup_hash), ("portable", portable, portable_hash)):
        entry = receipt.get(key) or {}
        if entry.get("name") != path.name or entry.get("sha256") != actual:
            _fail("RECEIPT_HASH_MISMATCH", f"{RECEIPT_NAME} {key} differs from verified bytes")

    # 版本三方一致：用户 tag == API 锚点版本 == 可信构建时版本。不是三者自洽，
    # 锚点来自来源 commit 的内容 API，构建时版本来自可信 job 现场。receipt 的
    # version 已在上面的逐字段对照里与来源比过，这里只剩 tag 一项。
    check_tag_matches_version(tag, source["version"])
    return {
        "setup": setup.name, "setup_sha256": setup_hash,
        "portable": portable.name, "portable_sha256": portable_hash,
        "version": source["version"], "run_id": source["run_id"],
        "run_attempt": source["run_attempt"], "head_sha": source["head_sha"],
    }


def resolve_tag(gh: list[str], repo: str, tag: str, source_sha: str) -> dict:
    """验收 D：精确 ref 查询明确 404 才表示新 tag；已有 tag 剥离核对来源 SHA。

    只认 ``refs/tags/<tag>`` 精确相等，前缀近似（如 v0.1.0 与 v0.1.0-rc1）
    不算命中。annotated 解引用的 404/403/500 一律拒绝，不当作不存在。
    """
    tag = validate_tag(tag)
    source_sha = validate_sha(source_sha, "head_sha")
    status, body = gh_api_include(gh, f"repos/{repo}/git/refs/tags/{tag}")
    if status == 404:
        return {"action": "create", "tag": tag, "target": source_sha}
    if status != 200:
        # 403/409/500/网络失败/无状态行一律拒绝：查不到绝不当作不存在。
        _fail("TAG_LOOKUP_FAILED", f"tag lookup for {tag} returned HTTP {status}")
    try:
        ref = json.loads(body)
    except json.JSONDecodeError:
        _fail("TAG_API_BAD_JSON", "tag lookup did not return JSON")
    if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag}":
        _fail("TAG_API_BAD_SHAPE", "tag lookup returned unexpected JSON")
    obj = ref.get("object") or {}
    obj_sha = obj.get("sha", "")
    obj_type = obj.get("type", "")
    if obj_type == "commit":
        pointed = validate_sha(str(obj_sha), "tag target")
    elif obj_type == "tag":
        try:
            tag_obj = gh_api_json(gh, repo, f"repos/{repo}/git/tags/{obj_sha}")
        except PublicationError as exc:
            _fail("TAG_PEEL_FAILED", f"annotated tag {tag} dereference failed: {exc}")
        inner = tag_obj.get("object") or {}
        if inner.get("type") != "commit":
            _fail("TAG_PEEL_FAILED", f"annotated tag {tag} does not peel to a commit")
        pointed = validate_sha(str(inner.get("sha", "")), "tag target")
    else:
        _fail("TAG_PEEL_FAILED", f"tag {tag} points to {obj_type!r}")
    if pointed != source_sha:
        _fail("TAG_POINTS_ELSEWHERE", f"tag {tag} points to {pointed}, source is {source_sha}")
    return {"action": "exists", "tag": tag, "target": pointed}


def build_release_argv(gh: list[str], repo: str, tag: str, source_sha: str, title: str,
                       notes_file: str, assets: list[str]) -> list[str]:
    """发布 argv：选项全部在 ``--`` 之前；仓库显式 ``--repo`` 已校验值。

    查询用对了 repo，写入也必须显式指定同一 repo，不能退回 cwd 推断或
    GH_REPO 环境（Reviewer 反例：GH_REPO 被污染时发布飞到别的仓库）。
    ``--`` 之后全是资产文件名；写成 ``create -- tag --target …`` 会把
    ``--target`` 当成要上传的文件名。tag 本身受 ``^vX.Y.Z$`` 约束，
    不会以 dash 开头。
    """
    if not repo or "/" not in repo:
        _fail("BAD_REPO", f"repo must be owner/name, got {repo!r}")
    return [
        *gh, "release", "create", "--repo", repo,
        tag, "--target", source_sha, "--title", title,
        "--notes-file", notes_file, "--", *assets,
    ]


def compose_notes(source: dict, tag: str) -> str:
    return (
        f"Windows 安装版（NSIS，未签名，校验见 SHA256SUMS 与 {RECEIPT_NAME}）。\n"
        f"构建来源：{source['repo']} desktop-windows 运行 {source['run_id']} "
        f"(attempt {source['run_attempt']})，commit {source['head_sha']}，版本 {source['version']}。\n"
        f"tag {tag} 已核对指向该 commit。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发布链最小闭环 helper（stdlib only）")
    parser.add_argument("--gh", default="gh", help="gh 可执行文件路径（测试可换 fake）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("assert-ref", help="发布 workflow 必须从 refs/heads/main 执行")
    p.add_argument("--ref", required=True)

    p = sub.add_parser("verify-run", help="核验构建来源并输出 source.json")
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("fetch-artifact", help="下载同 run 唯一未过期构件")
    p.add_argument("--repo", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--dest", required=True)

    p = sub.add_parser("verify-package", help="校验安装包/清单/来源清单/版本")
    p.add_argument("--dir", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--tag", required=True)

    p = sub.add_parser("write-receipt", help="构建侧生成来源清单")
    p.add_argument("--windows-dir", required=True)
    p.add_argument("--package-json", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-attempt", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("resolve-tag", help="解析 tag：create 或 exists（已核对 SHA）")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--sha", required=True)

    p = sub.add_parser("create-release", help="执行 gh release create（argv 列表，无 shell）")
    p.add_argument("--repo", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--package-dir", required=True,
                   help="已校验的构件根：发布前必跑同一完整 verify_package；"
                        "发布集合取 setup/portable/双 sidecar/完整 SHA256SUMS/来源清单，"
                        "与清单覆盖严格对应")
    p.add_argument("--title", default="")
    p.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gh = [args.gh]
    try:
        if args.command == "assert-ref":
            assert_trusted_ref(args.ref)
            print("ref ok: refs/heads/main")
        elif args.command == "verify-run":
            source = verify_run(gh, args.repo, args.run_id, args.ref)
            check_tag_matches_version(args.tag, source["version"])
            Path(args.out).write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(source, sort_keys=True))
        elif args.command == "fetch-artifact":
            source = json.loads(Path(args.source).read_text(encoding="utf-8"))
            info = fetch_artifact(gh, args.repo, source, args.dest)
            print(json.dumps(info, sort_keys=True))
        elif args.command == "verify-package":
            source = json.loads(Path(args.source).read_text(encoding="utf-8"))
            summary = verify_package(args.dir, source, args.tag)
            print(json.dumps(summary, sort_keys=True))
        elif args.command == "write-receipt":
            receipt = write_receipt(
                args.windows_dir, args.package_json, args.repo,
                args.run_id, args.run_attempt, args.head_sha, args.out,
            )
            print(json.dumps({"receipt": args.out, "run_id": receipt["run_id"],
                              "head_sha": receipt["head_sha"], "version": receipt["version"]}, sort_keys=True))
        elif args.command == "resolve-tag":
            print(json.dumps(resolve_tag(gh, args.repo, args.tag, args.sha), sort_keys=True))
        elif args.command == "create-release":
            source = json.loads(Path(args.source).read_text(encoding="utf-8"))
            if args.repo != source.get("repo"):
                _fail("REPO_CONFLICT", f"CLI repo {args.repo!r} != source repo {source.get('repo')!r}")
            tag = check_tag_matches_version(args.tag, source["version"])
            # 发布入口无旁路：先跑同一完整 verify_package（dry-run 同样验），
            # 再解析 tag，最后才组装发布 argv。
            summary = verify_package(args.package_dir, source, tag)
            plan = resolve_tag(gh, args.repo, tag, source["head_sha"])
            root = locate_package(Path(args.package_dir))
            setup = root / summary["setup"]
            portable = root / summary["portable"]
            assets = [str(setup), str(setup) + ".sha256", str(portable),
                      str(portable) + ".sha256", str(root / "SHA256SUMS"),
                      str(root / RECEIPT_NAME)]
            title = args.title or f"DeepDocParse {tag}（Windows 桌面版）"
            notes_file = str(Path(args.source).parent / "release-notes.txt")
            Path(notes_file).write_text(compose_notes(source, tag) + "\n", encoding="utf-8")
            cmd = build_release_argv(gh, args.repo, tag, source["head_sha"], title, notes_file, assets)
            if args.dry_run:
                print(json.dumps({"argv": cmd, "plan": plan}, sort_keys=True))
            else:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
                if done.returncode != 0:
                    _fail("RELEASE_CREATE_FAILED", f"gh release create exited {done.returncode}: {done.stderr.strip()[:300]}")
                print(done.stdout.strip())
    except PublicationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
