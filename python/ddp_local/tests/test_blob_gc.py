"""Local blob GC: mark-sweep over the content-addressed blob dir."""

import os
import time

import pytest

from ddp_local.runtime import LocalRuntime


def _hex(seed):
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def test_sweep_removes_only_old_unreferenced_hex(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        live_pdf = b"%PDF-live"
        live_key, _ = runtime.blobs.put_stream(__import__("io").BytesIO(live_pdf))
        runtime.store.create_resource(
            filename="live.pdf", blob_key=live_key, size=len(live_pdf),
            operation_key="gc-live")
        old = time.time() - 25 * 3600
        os.utime(live_key, (old, old), dir_fd=runtime.blobs.fd)
        orphan = _hex("orphan")
        with os.fdopen(os.open(orphan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                               dir_fd=runtime.blobs.fd), "wb") as stream:
            stream.write(b"orphan-bytes")
        os.utime(orphan, (old, old), dir_fd=runtime.blobs.fd)
        fresh = _hex("fresh")
        with os.fdopen(os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                               dir_fd=runtime.blobs.fd), "wb") as stream:
            stream.write(b"fresh-bytes")
        (runtime.blobs.directory / "notes.txt").write_bytes(b"do not touch")
        (runtime.blobs.directory / ".pending-abc").write_bytes(b"in flight")
        link_target = tmp_path / "outside.txt"
        link_target.write_bytes(b"outside")
        link_name = "f" * 64
        try:
            os.symlink(link_target, runtime.blobs.directory / link_name)
        except OSError:
            pass
        else:
            os.utime(link_name, (old, old), dir_fd=runtime.blobs.fd,
                     follow_symlinks=False)
        report = runtime.store.sweep_unreferenced_blobs(runtime.blobs, grace_seconds=24 * 3600)
        assert report["removed"] == 1, report
        assert report["bytes"] == len(b"orphan-bytes")
        assert report["live"] >= 1
        assert os.path.exists(os.path.join(f"/proc/self/fd/{runtime.blobs.fd}", live_key))
        assert os.path.exists(os.path.join(f"/proc/self/fd/{runtime.blobs.fd}", fresh))
        assert (runtime.blobs.directory / "notes.txt").is_file()
        assert (runtime.blobs.directory / ".pending-abc").is_file()
        assert link_target.read_bytes() == b"outside"
        assert (runtime.blobs.directory / link_name).is_symlink()
    finally:
        runtime.close()


def test_sweep_keeps_session_parts_and_bad_session_json(tmp_path):
    import io
    import json

    import pytest

    from ddp_core.application.ports import ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        staged_key, _ = runtime.blobs.put_stream(io.BytesIO(b"%PDF-staged"))
        session = runtime.store.create_upload_session(
            filename="staged.pdf", mime="application/pdf", declared_size=11,
            declared_sha256=None, target_resource_id=None, idempotency_key="gc-staged")
        runtime.store.db.execute(
            "UPDATE upload_sessions SET parts_json=? WHERE id=?",
            (json.dumps({"1": {"blob_key": staged_key, "size": 11}}), session["id"]))
        runtime.store.db.commit()
        old = time.time() - 25 * 3600
        os.utime(staged_key, (old, old), dir_fd=runtime.blobs.fd)
        report = runtime.store.sweep_unreferenced_blobs(runtime.blobs, grace_seconds=24 * 3600)
        assert report["removed"] == 0, report
        assert runtime.blobs.read(staged_key, 1024) == b"%PDF-staged"
        runtime.store.db.execute(
            "UPDATE upload_sessions SET parts_json='{{{corrupt' WHERE id=?", (session["id"],))
        runtime.store.db.commit()
        with pytest.raises(ApplicationError) as refused:
            runtime.store.sweep_unreferenced_blobs(runtime.blobs, grace_seconds=24 * 3600)
        assert refused.value.code == "upload_incomplete"
        assert runtime.blobs.read(staged_key, 1024) == b"%PDF-staged"
        runtime.store.db.execute(
            "UPDATE upload_sessions SET parts_json=? WHERE id=?",
            ("x" * (1024 * 1024 + 1), session["id"]))
        runtime.store.db.commit()
        with pytest.raises(ApplicationError) as oversized:
            runtime.store.sweep_unreferenced_blobs(runtime.blobs, grace_seconds=24 * 3600)
        assert oversized.value.code == "upload_incomplete"
        assert runtime.blobs.read(staged_key, 1024) == b"%PDF-staged"
    finally:
        runtime.close()


def test_sweep_claims_each_unlink_against_mid_sweep_commit(tmp_path):
    import io

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        payload = b"%PDF-relinked"
        relinked, _ = runtime.blobs.put_stream(io.BytesIO(payload))
        old = time.time() - 25 * 3600
        os.utime(relinked, (old, old), dir_fd=runtime.blobs.fd)

        def _commit_after_snapshot():
            runtime.store.create_resource(
                filename="relinked.pdf", blob_key=relinked, size=len(payload),
                operation_key="gc-claim")

        report = runtime.store.sweep_unreferenced_blobs(
            runtime.blobs, grace_seconds=24 * 3600,
            _after_snapshot=_commit_after_snapshot)
        assert report["removed"] == 0, report
        assert runtime.blobs.read(relinked, 1024) == payload
    finally:
        runtime.close()


def test_delete_version_reclaims_its_orphaned_blob_with_claim(tmp_path):
    import io

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        payload = b"%PDF-doomed"
        doomed, _ = runtime.blobs.put_stream(io.BytesIO(payload))
        created = runtime.store.create_resource(
            filename="doomed.pdf", blob_key=doomed, size=len(payload),
            operation_key="gc-entry", records=[], layout_key=None)
        version_id = created["version_id"]
        old = time.time() - 25 * 3600
        os.utime(doomed, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.store.delete_version(version_id, blobs=runtime.blobs)
        assert result["deleted"] is True
        assert result["blob_gc"]["removed"] == 1, result["blob_gc"]
        assert not os.path.exists(
            os.path.join(f"/proc/self/fd/{runtime.blobs.fd}", doomed))
        kept_payload = b"%PDF-kept"
        kept, _ = runtime.blobs.put_stream(io.BytesIO(kept_payload))
        created = runtime.store.create_resource(
            filename="kept.pdf", blob_key=kept, size=len(kept_payload),
            operation_key="gc-entry-kept", records=[], layout_key=None)
        os.utime(kept, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.store.delete_version(created["version_id"])
        assert "blob_gc" not in result
        assert runtime.blobs.read(kept, 1024) == kept_payload
    finally:
        runtime.close()


def test_production_delete_sweeps_orphan_and_keeps_referenced_blob(tmp_path):
    import io

    from ddp_core.application.ports import ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        doomed_payload = b"%PDF-doomed-production"
        doomed, _ = runtime.blobs.put_stream(io.BytesIO(doomed_payload))
        created = runtime.store.create_resource(
            filename="doomed.pdf", blob_key=doomed, size=len(doomed_payload),
            operation_key="gc-production-doomed", records=[], layout_key=None)
        kept_payload = b"%PDF-kept-production"
        kept, _ = runtime.blobs.put_stream(io.BytesIO(kept_payload))
        survivor = runtime.store.create_resource(
            filename="kept.pdf", blob_key=kept, size=len(kept_payload),
            operation_key="gc-production-kept", records=[], layout_key=None)
        old = time.time() - 25 * 3600
        os.utime(doomed, (old, old), dir_fd=runtime.blobs.fd)
        os.utime(kept, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.delete_version(created["version_id"])
        assert result["deleted"] is True
        assert result["blob_gc"]["removed"] == 1, result["blob_gc"]
        assert not os.path.exists(
            os.path.join(f"/proc/self/fd/{runtime.blobs.fd}", doomed))
        assert runtime.blobs.read(kept, 1024) == kept_payload
        with pytest.raises(ApplicationError) as gone:
            runtime.store.version(created["version_id"])
        assert gone.value.code == "not_found"
        assert runtime.store.version(survivor["version_id"])["id"] == survivor["version_id"]
    finally:
        runtime.close()


def test_production_delete_reports_sweep_refusal_without_failing(tmp_path):
    import io

    from ddp_core.application.ports import ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        payload = b"%PDF-fail-closed-production"
        key, _ = runtime.blobs.put_stream(io.BytesIO(payload))
        created = runtime.store.create_resource(
            filename="doomed.pdf", blob_key=key, size=len(payload),
            operation_key="gc-production-refused", records=[], layout_key=None)
        session = runtime.store.create_upload_session(
            filename="staged.pdf", mime="application/pdf", declared_size=11,
            declared_sha256=None, target_resource_id=None,
            idempotency_key="gc-production-corrupt")
        runtime.store.db.execute(
            "UPDATE upload_sessions SET parts_json='{{{corrupt' WHERE id=?", (session["id"],))
        runtime.store.db.commit()
        old = time.time() - 25 * 3600
        os.utime(key, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.delete_version(created["version_id"])
        assert result["deleted"] is True
        assert result["blob_gc"] == {"error": "upload_incomplete"}, result["blob_gc"]
        assert runtime.blobs.read(key, 1024) == payload
        with pytest.raises(ApplicationError) as gone:
            runtime.store.version(created["version_id"])
        assert gone.value.code == "not_found"
    finally:
        runtime.close()


def test_resource_command_delete_sweeps_after_receipt_commits(tmp_path):
    import io

    from ddp_core.application.ports import ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        payload = b"%PDF-command-sweep"
        key, _ = runtime.blobs.put_stream(io.BytesIO(payload))
        created = runtime.store.create_resource(
            filename="doomed.pdf", blob_key=key, size=len(payload),
            operation_key="gc-command", records=[], layout_key=None)
        old = time.time() - 25 * 3600
        os.utime(key, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.store.resource_command(
            "version.delete", created["version_id"],
            operation_key="gc-command-delete", blobs=runtime.blobs)
        assert result["deleted"] is True
        assert result["blob_gc"]["removed"] == 1, result["blob_gc"]
        assert not os.path.exists(
            os.path.join(f"/proc/self/fd/{runtime.blobs.fd}", key))
        replay = runtime.store.resource_command(
            "version.delete", created["version_id"],
            operation_key="gc-command-delete", blobs=runtime.blobs)
        assert replay == result
        with pytest.raises(ApplicationError) as gone:
            runtime.store.version(created["version_id"])
        assert gone.value.code == "not_found"
    finally:
        runtime.close()


def test_resource_command_delete_reports_sweep_refusal_in_receipt(tmp_path):
    import io

    from ddp_core.application.ports import ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        payload = b"%PDF-command-refused"
        key, _ = runtime.blobs.put_stream(io.BytesIO(payload))
        created = runtime.store.create_resource(
            filename="doomed.pdf", blob_key=key, size=len(payload),
            operation_key="gc-command-refused", records=[], layout_key=None)
        session = runtime.store.create_upload_session(
            filename="staged.pdf", mime="application/pdf", declared_size=11,
            declared_sha256=None, target_resource_id=None,
            idempotency_key="gc-command-corrupt")
        runtime.store.db.execute(
            "UPDATE upload_sessions SET parts_json='{{{corrupt' WHERE id=?", (session["id"],))
        runtime.store.db.commit()
        old = time.time() - 25 * 3600
        os.utime(key, (old, old), dir_fd=runtime.blobs.fd)
        result = runtime.store.resource_command(
            "version.delete", created["version_id"],
            operation_key="gc-command-refused-delete", blobs=runtime.blobs)
        assert result["deleted"] is True
        assert result["blob_gc"] == {"error": "upload_incomplete"}, result["blob_gc"]
        assert runtime.blobs.read(key, 1024) == payload
        replay = runtime.store.resource_command(
            "version.delete", created["version_id"],
            operation_key="gc-command-refused-delete", blobs=runtime.blobs)
        assert replay == result
        with pytest.raises(ApplicationError) as gone:
            runtime.store.version(created["version_id"])
        assert gone.value.code == "not_found"
    finally:
        runtime.close()
