"""Local composition root for shared DDP application workflows and offline adapters."""

import asyncio
import hashlib
import io
from pathlib import Path

from ddp_core.application.ports import ApplicationError
from ddp_core.application.workflows import KnowledgeApplication, compile_layout, evidence_records
from ddp_core.bundle import MAX_ARCHIVE, BundleError, build_bundle, json_bytes, read_bundle
from ddp_core.tokenize import tokens

from ddp_local.blobs import MAX_INPUT, FileBlobStore
from ddp_local.consents import ConsentStore
from ddp_local.providers import LocalExecutionProvider, ModelSelection
from ddp_local.model_runtime.install import ModelInstaller
from ddp_local.model_runtime.process import ModelProcess
from ddp_local.store import LocalStore
from ddp_local.wiki_store import LocalWikiStore


class LocalRuntime:
    def __init__(self, workspace: str | Path, *, model: ModelSelection | None = None):
        directory = Path(workspace).absolute()
        if directory.is_symlink():
            raise ApplicationError("unsafe_path", "workspace root cannot be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = LocalStore(directory)
        self.wikis = LocalWikiStore(self.store)
        self.blobs = FileBlobStore(directory / "blobs")
        self.consents = ConsentStore(
            directory, local_node_id=self.store.environment_id,
            input_resolver=lambda ref: self.blobs.read(self.store.version(ref)["blob_key"], MAX_INPUT),
            source_policy_resolver=self._consent_source_policies,
        )
        self.provider = LocalExecutionProvider(model)
        self._managed_selection = None
        self.model_installer = ModelInstaller(directory / "models", progress=self._model_progress)
        self.model_process = ModelProcess(self.model_installer, event=self._model_state)
        self.application = KnowledgeApplication(self.store, self.store, self.store, self.provider)

    def _consent_source_policies(self, scope):
        # Possessing an imported bundle is not authority to re-export its source.
        # The local adapter has no remote grant resolver, so inherited remote
        # authority cannot be relabeled with a default local owner grant.
        for item in scope["input_manifest"]:
            source = self.store.version(item["ref"]).get("source_json")
            if source and source.get("authority_node_id") != self.store.environment_id:
                return {"local:" + self.store.environment_id: {
                    "source_node_id": self.store.environment_id,
                    "allowed_recipients": [], "allowed_payload": [],
                    "allowed_retention": [], "valid_until": scope["plan"]["valid_until"],
                }}
        return {}

    def close(self):
        self.model_process.stop_sync()
        self.model_installer.close()
        self.consents.close()
        self.store.close()
        self.blobs.close()

    def _model_progress(self, value):
        with self.store.tx():
            self.store.event("model.install", None, value)

    def _model_state(self, value):
        if value["status"] == "failed" and self._managed_selection is not None:
            if self.provider.model is self._managed_selection:
                self.provider.model = None
        with self.store.tx():
            self.store.event("model.runtime", None, value)

    def models(self):
        return {"catalog_revision": self.model_installer.definitions["revision"],
                "items": [self.model_installer.status(a["id"]) for a in self.model_installer.definitions["artifacts"]],
                "runtime": self.model_process.status()}

    async def start_model(self, identifier):
        self.provider.model = await self.model_process.start(identifier)
        self._managed_selection = self.provider.model
        self._model_state(self.model_process.status())
        return self.model_process.status()

    async def stop_model(self):
        selected = self.model_process.selection
        result = await self.model_process.stop()
        if self.provider.model is selected:
            self.provider.model = None
        self._managed_selection = None
        self._model_state(result)
        return result

    def capabilities(self):
        managed = self.model_process.status()
        capabilities = self.provider.capabilities()
        if self._managed_selection is not None:
            artifact = self.model_installer.artifact(self.model_process.model_id)
            capabilities["generation"] = {
                "available": managed["status"] == "ready", "status": managed["status"],
                "model": artifact["id"], "location": "local", "revision": artifact["version"],
                "sha256": artifact["sha256"], "backend": artifact["backend"],
                "error": managed["error"],
            }
        return {
            "workspace_id": self.store.workspace_id,
            "environment_id": self.store.environment_id,
            "kind": "local",
            "accounts": False,
            **capabilities,
        }

    def client_handshake(self):
        return {
            "protocol_version": "ddp-client/1",
            "identity": {"environment_id": self.store.environment_id,
                         "workspace_id": self.store.workspace_id,
                         "authority_node_id": self.store.environment_id},
            "profile": {"issuer": self.store.environment_id,
                        "subject": "workspace:" + self.store.workspace_id},
            # API support is separate from capabilities() provider readiness.
            "capabilities": ["resource.list", "resource.upload", "task.read", "task.cancel",
                             "corpus.retrieve", "evidence.read", "bundle.export", "bundle.import",
                             "rag.answer.cited", "wiki.build", "client.snapshot", "client.events",
                             "client.receipt", "plan.prepare", "plan.approve", "plan.read", "plan.revoke",
                             "plan.dispatch", "plan.reconcile", "plan.federation.read", "plan.delivery.ack"],
        }

    @staticmethod
    def filename(filename):
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or any(c in filename for c in ("/", "\\", "\x00", "\n", "\r"))
            or filename in {".", ".."}
        ):
            raise ApplicationError("invalid_filename", "a plain filename is required")
        return filename

    def upload_file(self, filename: str, *, operation_key: str):
        name = self.filename(Path(filename).name)
        key, size = self.blobs.snapshot(filename)
        return self.store.create_resource(
            filename=name, blob_key=key, size=size, operation_key=operation_key
        )

    def upload_stream(self, stream, *, filename: str, operation_key: str):
        name = self.filename(filename)
        key, size = self.blobs.put_stream(stream)
        return self.store.create_resource(
            filename=name, blob_key=key, size=size, operation_key=operation_key
        )

    def source(self, version):
        return {
            "origin_node_id": self.store.environment_id,
            "authority_node_id": self.store.environment_id,
            "resource_id": version["resource_id"],
            "source_version_id": version["id"],
            "source_digest": "sha256:" + version["source_digest"],
            "parse_revision": version["parse_revision"],
            "filename": version["filename"],
            "mime": "application/pdf",
            "original": "present",
            "missing_reason": None,
            "uploader_ref": "workspace:" + self.store.workspace_id,
            "policy_revision": "local-private/1",
        }

    async def _execute(self, task):
        version = self.store.version(task["version_id"])
        layout = await self.provider.parse(self.blobs.path(version["blob_key"]))
        compiled = compile_layout(
            layout,
            parse_options_hash=hashlib.sha256(
                json_bytes({"engine": "borndigital", "code_detection": "heuristic"})
            ).hexdigest(),
            embedding_model="disabled",
            vision_model="disabled",
        )
        if not compiled.chunks:
            raise ApplicationError("no_text_layer", "this CPU profile requires a PDF text layer")
        records = evidence_records(compiled.chunks, self.source(version))
        wrapped = {
            "schema": "ddp-bundle-layout/1",
            "state": "present",
            "layout": layout,
            "reason": None,
        }
        files = {
            "source.bin": self.blobs.read(version["blob_key"], MAX_INPUT),
            "layout.json": json_bytes(wrapped),
            "evidence.json": json_bytes(
                [{k: r[k] for k in ("evidence", "excerpt")} for r in records]
            ),
            "provenance.json": json_bytes(
                [{"provider": compiled.provider, "execution": "cpu_subprocess"}]
            ),
        }
        bundle = build_bundle(self.source(version), files)
        layout_key, bundle_key = self.blobs.write(files["layout.json"]), self.blobs.write(bundle)
        return self.store.publish_parse(
            task,
            layout_key=layout_key,
            bundle_key=bundle_key,
            records=records,
            provider=compiled.provider,
            degraded=[*compiled.degraded, "embedding_unavailable", "vision_unavailable"],
        )

    async def work_once(self):
        task = self.store.claim()
        if task is None:
            return None

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                if not self.store.renew(task["id"], task["generation"]):
                    return False

        execution, pulse = (
            asyncio.create_task(self._execute(task)),
            asyncio.create_task(heartbeat()),
        )
        try:
            finished, _ = await asyncio.wait(
                {execution, pulse}, return_when=asyncio.FIRST_COMPLETED
            )
            if pulse in finished and not pulse.result():
                execution.cancel()
            else:
                await execution
        except (ApplicationError, BundleError) as exc:
            self.store.fail(task, exc.code, str(exc))
        except Exception as exc:
            self.store.fail(task, "parse_failed", type(exc).__name__)
        finally:
            for child in (execution, pulse):
                child.cancel()
            await asyncio.gather(execution, pulse, return_exceptions=True)
        return self.store.task(task["id"])

    async def work_forever(self):
        while True:
            if await self.work_once() is None:
                await asyncio.sleep(0.25)

    def search(self, query, *, version_ids=None, limit=10):
        return self.application.search(query, version_ids=version_ids, limit=limit)

    def _provider_intent(self):
        model = self.provider.model
        if model is None:
            return None
        if model.provenance and model.provenance.get("model_id"):
            return {"location": model.location, "model_id": model.provenance["model_id"],
                    "model_sha256": model.provenance.get("model_sha256")}
        return {"location": model.location, "model": model.model, "endpoint": model.endpoint}

    async def answer(
        self,
        query,
        *,
        version_ids=None,
        execution_policy="local_only",
        allow_remote=False,
        wiki=False,
        operation_key=None,
    ):
        from ddp_local.store import new_id

        selected = (
            version_ids
            if version_ids is not None
            else [v["id"] for v in self.store.versions() if v["state"] == "ready"]
        )
        # Freeze the scope before creating the durable request. Provider selection is
        # persisted before any possible network call, including remote consent.
        request = {
            "query": query,
            "version_ids": selected,
            "execution_policy": execution_policy,
            "allow_remote": allow_remote,
            "outbound_payload": ["question", "selected_evidence"],
            "provider_intent": self._provider_intent(),
        }
        async def operation(task):
            return await self.application.answer(query, version_ids=selected,
                execution_policy=execution_policy, allow_remote=allow_remote, wiki=wiki)
        def publish(task, result):
            if not wiki or not result.get("assertions"):
                return {}
            from ddp_core.application.wiki import page_key
            from ddp_core.knowledge import wiki_sentence
            page = {"page_key": page_key(query), "title": query, "generated_sections": [{"heading": query,
                "sentences": [{**wiki_sentence(text=a["text"], evidence_ids=a["evidence_ids"], provider=result["provider"]),
                               "id": new_id(), "source_type": "generated"} for a in result["assertions"]]}],
                "human_paragraphs": []}
            saved = self.wikis.publish(task, title=query,
                result={**result, "pages": [page], "limits": {"max_pages": 1}}, frozen=result["evidence"])
            return {**saved, "wiki_id": saved["wiki"]["id"], "revision_id": saved["revision"]["id"]}
        return await self._persistent("wiki" if wiki else "answer", request, operation_key or new_id(), operation, publish)

    async def _persistent(self, kind, request, key, operation, publish=None, *, replace_result=False):
        task, created = self.store.begin_generation(kind, request, key, selection={
            "generation": self.provider.capabilities()["generation"],
            "endpoint": self.provider.model.endpoint if self.provider.model else None})
        if not created:
            return self.store.generation_result(task)

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                if not self.store.renew(task["id"], task["generation"]):
                    return

        execution = asyncio.create_task(operation(task))
        pulse = asyncio.create_task(heartbeat())
        try:
            finished, _ = await asyncio.wait(
                {execution, pulse}, return_when=asyncio.FIRST_COMPLETED
            )
            if pulse in finished:
                raise ApplicationError(
                    "execution_cancelled", "generation was cancelled or superseded"
                )
            return self.store.finish_generation(task, await execution, publish=publish, replace_result=replace_result)
        except (ApplicationError, BundleError) as exc:
            self.store.fail(task, exc.code, str(exc))
            raise
        except asyncio.CancelledError:
            self.store.fail(task, "execution_interrupted", "runtime stopped during generation")
            raise
        except Exception as exc:
            self.store.fail(task, "generation_failed", type(exc).__name__)
            raise ApplicationError("generation_failed", "generation could not complete") from exc
        finally:
            execution.cancel()
            pulse.cancel()
            await asyncio.gather(execution, pulse, return_exceptions=True)

    async def model_operation(self, action, identifier=None, *, operation_key):
        if action not in {"install", "start", "stop"}:
            raise ApplicationError("invalid_model_operation", "unsupported managed model operation")
        if action != "stop":
            self.model_installer.artifact(identifier)
        async def execute(task):
            if action == "install":
                return await self.model_installer.download(identifier)
            if action == "start":
                return await self.start_model(identifier)
            return await self.stop_model()
        return await self._persistent("model_" + action, {"action": action, "artifact_id": identifier},
                                      operation_key, execute)

    async def build_wiki(self, body, *, operation_key, wiki_id=None):
        import copy
        body = copy.deepcopy(body)
        from ddp_core.application.wiki import generate_wiki, limits_for, text_field
        title = text_field(body.get("title"), 255)
        limits = limits_for(body)
        base = body.get("base_revision_id")
        # Base is checked again at publication under BEGIN IMMEDIATE.
        async def execute(task):
            if wiki_id:
                self.wikis.check_base(wiki_id, base)
            elif base is not None:
                raise ApplicationError("revision_conflict", "a new Wiki has no base revision")
            frozen = self.wikis.freeze(body.get("sources"), limits)
            selected["frozen"] = frozen
            return await generate_wiki(self.provider, title, frozen, limits,
                execution_policy=body.get("execution_policy", "local_only"),
                allow_remote=body.get("allow_remote", False),
                record_attempt=lambda stage, messages, allowance: self.wikis.attempt(task, stage, messages, allowance))
        selected = {}
        def publish(task, result):
            return self.wikis.publish(task, title=title, result=result, frozen=selected["frozen"],
                                      wiki_id=wiki_id, base_revision_id=base)
        return await self._persistent("wiki", {"operation": "wiki_build", "wiki_id": wiki_id, "body": body,
                                               "provider_intent": self._provider_intent()},
                                      operation_key, execute, publish, replace_result=True)

    async def edit_wiki(self, wiki_id, page_key, body, *, operation_key):
        import copy
        body = copy.deepcopy(body)
        from ddp_core.application.wiki import text_field
        selected = {}
        async def execute(task):
            prior = self.wikis.check_base(wiki_id, body["base_revision_id"])
            if prior["stale"]:
                raise ApplicationError("wiki_source_unavailable", "rebuild stale sources before editing this draft")
            paragraphs = body.get("paragraphs")
            if not isinstance(paragraphs, list) or len(paragraphs) > 100:
                raise ApplicationError("wiki_budget_exceeded", "human paragraph budget exceeded")
            clean = [{"id": text_field(p.get("id"), 64), "text": text_field(p.get("text")),
                      "kind": "human", "source_type": "generated", "unsupported": True,
                      "evidence_ids": [], "review_state": "unreviewed",
                      "created_by": "workspace:" + self.store.workspace_id} for p in paragraphs]
            if len({p["id"] for p in clean}) != len(clean):
                raise ApplicationError("duplicate_paragraph", "human paragraph IDs must be unique")
            pages = copy.deepcopy(prior["pages"])
            target = next((p for p in pages if p["page_key"] == page_key), None)
            if target is None:
                raise ApplicationError("not_found", "page not found in this Wiki revision")
            selected["edit"] = {"page_key": page_key, "before": target["human_paragraphs"], "after": clean}
            target["human_paragraphs"] = clean
            ids = {d["evidence_id"] for d in prior["dependency_manifest"]}
            selected["frozen"] = [self.store.evidence(eid) for eid in sorted(ids)]
            return {**prior, "pages": pages}
        def publish(task, result):
            return self.wikis.publish(task, title=result["title"], result=result, frozen=selected["frozen"],
                wiki_id=wiki_id, base_revision_id=body["base_revision_id"], kind="human_edit", edit=selected["edit"])
        return await self._persistent("wiki", {"operation": "wiki_edit", "wiki_id": wiki_id,
            "page_key": page_key, "body": body}, operation_key, execute, publish, replace_result=True)

    def export_bundle(self, version_id):
        version = self.store.version(version_id)
        if version["state"] not in {"ready", "unparsed"} or not version["bundle_key"]:
            raise ApplicationError(
                "version_not_ready", "wait for the fixed parse to finish before export"
            )
        data = self.blobs.read(version["bundle_key"], MAX_ARCHIVE)
        read_bundle(io.BytesIO(data))
        return data

    def source_bytes(self, version_id):
        self.store.authorize_versions([version_id])
        version = self.store.version(version_id)
        if version.get("source_json") and version["source_json"].get("original") != "present":
            raise ApplicationError("source_missing", "original source bytes are unavailable")
        data = self.blobs.read(version["blob_key"], MAX_INPUT)
        if not data.startswith(b"%PDF-"):
            raise ApplicationError("source_invalid", "fixed source is not a supported PDF")
        return data

    def import_bundle(self, stream, *, operation_key):
        verified = read_bundle(stream)
        if verified.source["original"] != "present":
            raise ApplicationError(
                "source_missing", "this local workspace requires the original source bytes"
            )
        if len(verified.files["source.bin"]) > MAX_INPUT:
            raise ApplicationError("input_too_large", "source exceeds the local CPU input budget")
        # Immutable remote identities are retained only inside verified evidence envelopes.
        # Local resource/database keys are always newly allocated by this workspace.
        records = []
        for record in verified.evidence:
            e = record["evidence"]
            if e["source_type"] != "source":
                continue
            loc, excerpt = e["locator"], record["excerpt"]
            size = loc["page_size"]
            chunk = {
                "seq": loc["seq"],
                "text": excerpt,
                "text_tokenized": " ".join(tokens(excerpt)),
                "page_idx": loc["physical_page_index"],
                "bbox": loc["bbox"],
                "page_size": [size["width"], size["height"]] if size else None,
                "block_type": e["block_type"],
                "source_type": "source",
                "model_meta": {"origin": "verified_bundle", "vector_available": False},
            }
            records.append({**record, "chunk": chunk})
        source_key = self.blobs.write(verified.files["source.bin"])
        bundle_key = self.blobs.write(build_bundle(verified.source, verified.files))
        layout_key = self.blobs.write(verified.files["layout.json"])
        return self.store.create_resource(
            filename=self.filename(verified.source["filename"]),
            blob_key=source_key,
            size=len(verified.files["source.bin"]),
            operation_key=operation_key,
            records=records,
            bundle_key=bundle_key,
            source=verified.source,
            layout_key=layout_key,
        )

    # -- P5 联邦派发（App 侧）：许可门、中心写请求与持久对账投影 ----------------

    async def federation_dispatch(self, plan_id, config, *, phase, operation_key=None,
                                  actor_headers=None, scope_manifest=None, center_ref=None):
        from ddp_local.federation_dispatch import dispatch_plan

        return await dispatch_plan(self, plan_id, config, phase=phase, operation_key=operation_key,
                                   actor_headers=actor_headers, scope_manifest=scope_manifest,
                                   center_ref=center_ref)

    async def federation_reconcile(self, plan_id, config, *, actor_headers=None):
        from ddp_local.federation_dispatch import reconcile

        return await reconcile(self, plan_id, config, actor_headers=actor_headers)

    async def federation_fetch_delivery(self, plan_id, config, *, actor_headers=None):
        from ddp_local.federation_dispatch import fetch_delivery

        return await fetch_delivery(self, plan_id, config, actor_headers=actor_headers)

    async def federation_confirm_delivery(self, plan_id, delivery_id, result_manifest_digest,
                                          config, *, actor_headers=None):
        from ddp_local.federation_dispatch import confirm_delivery

        return await confirm_delivery(self, plan_id, delivery_id, result_manifest_digest, config,
                                      actor_headers=actor_headers)

    def federation_state(self, plan_id):
        from ddp_local.federation_dispatch import load_federation_state

        return load_federation_state(self, plan_id)
