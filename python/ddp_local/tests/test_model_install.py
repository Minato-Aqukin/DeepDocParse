import asyncio
import hashlib
import threading
import os

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.model_runtime.install import ModelInstaller


PAYLOAD = b"GGUF\x03\0\0\0" + b"test-fixture-only" * 100


@pytest.fixture
def installer(tmp_path):
    definitions = {
        "schema": "ddp-model-catalog/1", "revision": "test-only",
        "artifacts": [{"id": "test-model", "kind": "model", "name": "fixture",
                       "version": "fixed", "filename": "fixture.gguf", "bytes": len(PAYLOAD),
                       "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "license": "Apache-2.0",
                       "license_url": "https://model.example/license", "url": "https://model.example/fixed.gguf",
                       "format": "gguf-v3", "backend": "llama.cpp", "device": "cpu"}],
    }
    value = ModelInstaller(tmp_path / "models", definitions=definitions)
    yield value
    value.close()


def test_import_needs_exact_digest_and_partial_never_becomes_ready(installer, tmp_path):
    source = tmp_path / "wrong.gguf"
    source.write_bytes(PAYLOAD[:-1] + b"x")
    with pytest.raises(ApplicationError) as bad:
        installer.import_file("test-model", source)
    assert bad.value.code == "model_digest_mismatch"
    assert installer.status("test-model")["status"] == "partial"
    source.write_bytes(PAYLOAD)
    assert installer.import_file("test-model", source)["status"] == "installed"
    name = installer.name(installer.artifact("test-model"))
    with open(installer.directory / name, "r+b") as out:
        out.seek(9)
        out.write(b"!")
    assert installer.status("test-model")["status"] == "verification_required"
    with pytest.raises(ApplicationError) as corrupt:
        installer.verify("test-model")
    assert corrupt.value.code == "model_digest_mismatch"


def test_model_catalog_unknown_path_and_symlink_fail_closed(installer, tmp_path):
    with pytest.raises(ApplicationError):
        installer.artifact("../../outside")
    outside = tmp_path / "outside"
    outside.write_bytes(PAYLOAD)
    link = tmp_path / "source-link"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        installer.import_file("test-model", link)
    target = installer.directory / installer.name(installer.artifact("test-model"))
    target.symlink_to(outside)
    with pytest.raises(OSError):
        installer.status("test-model")
    assert outside.read_bytes() == PAYLOAD


async def test_explicit_download_resumes_partial_and_validates_range(installer):
    artifact = installer.artifact("test-model")
    partial = installer.directory / (installer.name(artifact) + ".part")
    partial.write_bytes(PAYLOAD[:100])

    def transport(request):
        assert request.headers["range"] == "bytes=100-"
        return httpx.Response(206, content=PAYLOAD[100:],
                              headers={"Content-Range": f"bytes 100-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await installer.download("test-model", client=client)
    assert result["status"] == "installed"
    assert not partial.exists()
    assert installer.verify("test-model")["status"] == "installed"


async def test_server_ignoring_range_restarts_instead_of_appending(installer):
    artifact = installer.artifact("test-model")
    (installer.directory / (installer.name(artifact) + ".part")).write_bytes(PAYLOAD[:100])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PAYLOAD))) as client:
        assert (await installer.download("test-model", client=client))["status"] == "installed"


async def test_wrong_range_and_publisher_redirect_cannot_publish(installer):
    artifact = installer.artifact("test-model")
    (installer.directory / (installer.name(artifact) + ".part")).write_bytes(PAYLOAD[:100])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        206, content=PAYLOAD[100:], headers={"Content-Range": f"bytes 99-{len(PAYLOAD)-1}/{len(PAYLOAD)}"}
    ))) as client:
        with pytest.raises(ApplicationError) as wrong:
            await installer.download("test-model", client=client)
        assert wrong.value.code == "model_download_range"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        302, headers={"Location": "https://127.0.0.1/private"}
    ))) as client:
        with pytest.raises(ApplicationError) as redirect:
            await installer.download("test-model", client=client)
        assert redirect.value.code == "model_download_redirect"
    assert installer.status("test-model")["status"] == "failed"


async def test_interrupted_transfer_keeps_resumable_bytes(installer):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PAYLOAD[:100]
            raise httpx.ReadError("simulated broken transfer")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Broken()))) as client:
        with pytest.raises(ApplicationError) as interrupted:
            await installer.download("test-model", client=client)
    assert interrupted.value.code == "model_download_failed"
    status = installer.status("test-model")
    assert status["status"] == "failed" and status["downloaded_bytes"] == 100


def test_installer_lock_prevents_two_publishers(installer):
    artifact = installer.artifact("test-model")
    with installer.lock(artifact):
        with pytest.raises(ApplicationError) as busy:
            with installer.lock(artifact):
                pass
        assert busy.value.code == "model_install_busy"


def test_model_directory_requires_private_owned_permissions(installer, tmp_path):
    directory = tmp_path / "public-models"
    directory.mkdir(mode=0o755)
    with pytest.raises(ApplicationError) as unsafe:
        ModelInstaller(directory, definitions=installer.definitions)
    assert unsafe.value.code == "unsafe_path"


def test_import_does_not_truncate_a_crafted_partial_hardlink(installer, tmp_path):
    source = tmp_path / "selected.gguf"
    source.write_bytes(PAYLOAD)
    outside = tmp_path / "unrelated-data"
    outside.write_bytes(b"keep me")
    partial = installer.directory / (installer.name(installer.artifact("test-model")) + ".part")
    os.link(outside, partial)
    with pytest.raises(ApplicationError) as unsafe:
        installer.import_file("test-model", source)
    assert unsafe.value.code == "unsafe_path"
    assert outside.read_bytes() == b"keep me"


async def test_cancelled_download_waits_for_verifier_before_closing_descriptor(installer, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    publish = installer._publish

    def slow_publish(artifact, descriptor):
        entered.set()
        assert release.wait(5)
        publish(artifact, descriptor)

    monkeypatch.setattr(installer, "_publish", slow_publish)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PAYLOAD))) as client:
        task = asyncio.create_task(installer.download("test-model", client=client))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "the active verifier still owns its descriptor"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert installer.status("test-model")["status"] == "installed"
