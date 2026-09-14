"""CLI over the same local application runtime used by the desktop transport."""

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import sys
import uuid
from pathlib import Path

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import BundleError

from ddp_local.federation_client import CenterConfig, CenterFault
from ddp_local.providers import ModelSelection
from ddp_local.runtime import LocalRuntime


def add_center_args(command):
    command.add_argument("--center-ref", help="locally configured center reference")
    command.add_argument("--endpoint", help="inline center endpoint (HTTPS, no trailing slash)")
    command.add_argument("--credential-env", help="environment variable holding the center credential")
    command.add_argument("--allow-loopback", action="store_true",
                         help="explicit test-only HTTP loopback for a literal 127.0.0.1/[::1] endpoint")


def center_from_args(runtime, args, plan_id):
    from ddp_local.federation_dispatch import resolve_center_ref

    if getattr(args, "center_ref", None):
        return resolve_center_ref(runtime, args.center_ref)
    if getattr(args, "endpoint", None):
        credential = os.environ.get(args.credential_env) if args.credential_env else None
        return CenterConfig(endpoint=args.endpoint, credential=credential or "",
                            allow_loopback=getattr(args, "allow_loopback", False))
    try:
        state = runtime.federation_state(plan_id)
    except ApplicationError:
        state = None
    ref = state.get("center_ref") if state else None
    if ref:
        return resolve_center_ref(runtime, ref)
    raise ApplicationError("invalid_plan", "--endpoint or --center-ref is required")


def load_scope_manifest(path):
    if not path:
        return None
    with open(path, "rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ApplicationError("input_too_large", "scope manifest exceeds 64 KiB")
    return json.loads(data)


def parser():
    root = argparse.ArgumentParser(prog="ddp-local")
    root.add_argument("--workspace", required=True)
    root.add_argument("--model-endpoint")
    root.add_argument("--model")
    root.add_argument("--model-location", choices=["local", "remote"], default="local")
    root.add_argument(
        "--model-key-env", help="explicit environment variable holding this provider API key"
    )
    sub = root.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models")
    actions = models.add_subparsers(dest="model_action", required=True)
    actions.add_parser("list")
    actions.add_parser("status")
    for action in ("install", "verify", "run"):
        command = actions.add_parser(action)
        command.add_argument("artifact_id")
        if action == "install":
            command.add_argument("--key", required=True)
    imported = actions.add_parser("import")
    imported.add_argument("artifact_id")
    imported.add_argument("file")
    wikis = sub.add_parser("wikis")
    wiki_actions = wikis.add_subparsers(dest="wiki_action", required=True)
    list_wikis = wiki_actions.add_parser("list")
    list_wikis.add_argument("--limit", type=int, default=50)
    list_wikis.add_argument("--cursor")
    get_wiki = wiki_actions.add_parser("get")
    get_wiki.add_argument("wiki_id")
    get_wiki.add_argument("--revision")
    revisions = wiki_actions.add_parser("revisions")
    revisions.add_argument("wiki_id")
    revisions.add_argument("--limit", type=int, default=50)
    revisions.add_argument("--cursor")
    for action in ("build", "rebuild", "edit"):
        command = wiki_actions.add_parser(action)
        command.add_argument("--body", required=True, help="explicit JSON request file")
        command.add_argument("--key", required=True)
        if action != "build":
            command.add_argument("wiki_id")
        if action == "edit":
            command.add_argument("page_key")
    for command in ("init", "capabilities", "resources", "tasks"):
        sub.add_parser(command)
    upload = sub.add_parser("upload")
    upload.add_argument("file")
    upload.add_argument("--key", required=True)
    work = sub.add_parser("work")
    work.add_argument("--once", action="store_true")
    for command in ("search", "answer", "wiki"):
        query = sub.add_parser(command)
        query.add_argument("query")
        query.add_argument("--version", action="append", dest="version_ids")
        if command != "search":
            query.add_argument(
                "--allow-remote",
                action="store_true",
                help="send the question and retrieved source excerpts to the selected remote model",
            )
    for command in ("evidence", "cancel"):
        sub.add_parser(command).add_argument("id")
    export = sub.add_parser("export")
    export.add_argument("version_id")
    export.add_argument("--output", required=True)
    ingest = sub.add_parser("import")
    ingest.add_argument("file")
    ingest.add_argument("--key", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token-file")
    serve.add_argument("--installed-model", help="explicitly start this verified catalog model with the listener")
    federation = sub.add_parser("federation", help="dispatch an approved plan to a center and reconcile")
    federation_actions = federation.add_subparsers(dest="federation_action", required=True)
    federation_state = federation_actions.add_parser("state", help="print the persisted center projection")
    federation_state.add_argument("--plan-id", required=True)
    federation_dispatch = federation_actions.add_parser(
        "dispatch", help="send one approved phase through the consent gate"
    )
    federation_dispatch.add_argument("--plan-id", required=True)
    federation_dispatch.add_argument("--phase", choices=["exploration", "execution"], required=True)
    federation_dispatch.add_argument("--key", help="local Idempotency-Key; a retry with the same key never re-sends")
    federation_dispatch.add_argument("--scope-manifest", help="explicit JSON file with the P4 scope manifest")
    add_center_args(federation_dispatch)
    federation_reconcile = federation_actions.add_parser(
        "reconcile", help="read center task/coverage and update the local projection; never replays writes"
    )
    federation_reconcile.add_argument("--plan-id", required=True)
    add_center_args(federation_reconcile)
    federation_fetch = federation_actions.add_parser(
        "fetch-delivery", help="refresh delivery state and verify the center result digest"
    )
    federation_fetch.add_argument("--plan-id", required=True)
    add_center_args(federation_fetch)
    federation_ack = federation_actions.add_parser(
        "delivery-ack", help="confirm a locally verified delivery"
    )
    federation_ack.add_argument("--plan-id", required=True)
    federation_ack.add_argument("--delivery-id", required=True)
    federation_ack.add_argument("--digest", required=True, help="sha256:... result manifest digest")
    add_center_args(federation_ack)
    return root


def serve(runtime, args):
    import uvicorn

    from ddp_local.http import create_app

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    token = secrets.token_urlsafe(48)
    token_path = None
    if args.token_file != "-":
        token_path = Path(
            args.token_file or (Path(args.workspace) / ("session-" + uuid.uuid4().hex + ".json"))
        )

        # A SIGTERM between file creation and uvicorn's own signal handlers
        # would take the default action (terminate) and leave the session
        # credential on disk. Install the cleanup handler *before* the file
        # exists; uvicorn overrides it once it starts, and its graceful path
        # still runs the lifespan shutdown + finally unlink.
        def remove_token_on_signal(signum, _frame):
            try:
                token_path.unlink(missing_ok=True)
            except OSError:
                pass
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, remove_token_on_signal)
        signal.signal(signal.SIGINT, remove_token_on_signal)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"url": f"http://127.0.0.1:{port}", "token": token, "pid": os.getpid()}, out)
            out.flush()
            os.fsync(out.fileno())
    try:
        # Uvicorn may re-raise SIGTERM after ASGI shutdown, before the outer
        # finally runs. Remove the session credential within lifespan shutdown.
        bootstrap = {"url": f"http://127.0.0.1:{port}", "pid": os.getpid()}
        if token_path is None:
            bootstrap["token"] = token
        else:
            bootstrap["token_file"] = str(token_path.absolute())
        app = create_app(
            runtime, session_token=token, allowed_hosts={f"127.0.0.1:{port}"},
            on_shutdown=(lambda: token_path.unlink(missing_ok=True)) if token_path else None,
        )
        print(
            json.dumps({**bootstrap, **runtime.client_handshake()}),
            flush=True,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        )
        async def run_server():
            if args.installed_model:
                await runtime.start_model(args.installed_model)
            try:
                await server.serve(sockets=[listener])
            finally:
                await runtime.stop_model()
        asyncio.run(run_server())
    finally:
        listener.close()
        if token_path is not None:
            token_path.unlink(missing_ok=True)


def main(argv=None):
    args = parser().parse_args(argv)
    runtime = None
    try:
        model = None
        if args.model_endpoint or args.model:
            if not (args.model_endpoint and args.model):
                raise ApplicationError(
                    "invalid_provider", "--model and --model-endpoint must be selected together"
                )
            model = ModelSelection(
                args.model_endpoint,
                args.model,
                args.model_location,
                os.environ.get(args.model_key_env) if args.model_key_env else None,
            )
        runtime = LocalRuntime(args.workspace, model=model)
        command = args.command
        if command == "models":
            action = args.model_action
            if action in {"list", "status"}:
                result = runtime.models()
            elif action == "install":
                persist_progress = runtime.model_installer.progress
                def report_progress(value):
                    persist_progress(value)
                    print(json.dumps(value), file=sys.stderr, flush=True)
                runtime.model_installer.progress = report_progress
                result = asyncio.run(runtime.model_operation("install", args.artifact_id, operation_key=args.key))
            elif action == "verify":
                result = runtime.model_installer.verify(args.artifact_id)
            elif action == "import":
                result = runtime.model_installer.import_file(args.artifact_id, args.file)
            else:
                async def foreground_model():
                    result = await runtime.start_model(args.artifact_id)
                    print(json.dumps(result), flush=True)
                    try:
                        while runtime.model_process.process.poll() is None:
                            await asyncio.sleep(0.25)
                    finally:
                        await runtime.stop_model()
                asyncio.run(foreground_model())
                return 0
        elif command == "wikis":
            action = args.wiki_action
            if action == "list":
                result = runtime.wikis.list(limit=args.limit, cursor=args.cursor)
            elif action == "get":
                result = runtime.wikis.get(args.wiki_id, args.revision)
            elif action == "revisions":
                result = runtime.wikis.revisions(args.wiki_id, limit=args.limit, cursor=args.cursor)
            else:
                with open(args.body, "rb") as source:
                    data = source.read(65537)
                if len(data) > 65536:
                    raise ApplicationError("input_too_large", "Wiki request exceeds 64 KiB")
                body = json.loads(data)
                if action == "edit":
                    result = asyncio.run(runtime.edit_wiki(args.wiki_id, args.page_key, body, operation_key=args.key))
                else:
                    result = asyncio.run(runtime.build_wiki(body, operation_key=args.key,
                                                           wiki_id=args.wiki_id if action == "rebuild" else None))
        elif command in {"init", "capabilities"}:
            result = runtime.capabilities()
        elif command == "resources":
            result = {"items": runtime.store.versions()}
        elif command == "tasks":
            result = {"items": runtime.store.tasks()}
        elif command == "upload":
            result = runtime.upload_file(args.file, operation_key=args.key)
        elif command == "work":
            result = asyncio.run(runtime.work_once() if args.once else runtime.work_forever())
        elif command == "search":
            result = runtime.search(args.query, version_ids=args.version_ids)
        elif command in {"answer", "wiki"}:
            result = asyncio.run(
                runtime.answer(
                    args.query,
                    version_ids=args.version_ids,
                    wiki=command == "wiki",
                    execution_policy="remote_allowed" if args.allow_remote else "local_only",
                    allow_remote=args.allow_remote,
                )
            )
        elif command == "evidence":
            result = runtime.store.evidence(args.id)
        elif command == "cancel":
            result = runtime.store.cancel(args.id)
        elif command == "export":
            data = runtime.export_bundle(args.version_id)
            # Explicit destination; do not silently replace an existing user file.
            with open(args.output, "xb") as out:
                out.write(data)
            result = {"path": str(Path(args.output).absolute()), "bytes": len(data)}
        elif command == "import":
            fd = os.open(args.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                result = runtime.import_bundle(stream, operation_key=args.key)
        elif command == "federation":
            action = args.federation_action
            if action == "state":
                result = runtime.federation_state(args.plan_id)
            else:
                config = center_from_args(runtime, args, args.plan_id)
                if action == "dispatch":
                    result = asyncio.run(runtime.federation_dispatch(
                        args.plan_id, config, phase=args.phase, operation_key=args.key,
                        scope_manifest=load_scope_manifest(args.scope_manifest),
                        center_ref=args.center_ref))
                elif action == "reconcile":
                    result = asyncio.run(runtime.federation_reconcile(args.plan_id, config))
                elif action == "fetch-delivery":
                    result = asyncio.run(runtime.federation_fetch_delivery(args.plan_id, config))
                else:
                    result = asyncio.run(runtime.federation_confirm_delivery(
                        args.plan_id, args.delivery_id, args.digest, config))
        else:
            serve(runtime, args)
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except CenterFault as exc:
        print(json.dumps({"error": {"code": exc.code, "status": exc.status,
                                    "retryable": exc.retryable}}))
        return 1
    except (ApplicationError, BundleError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        return 1
    except OSError as exc:
        print(json.dumps({"error": {"code": "filesystem_error", "message": type(exc).__name__}}))
        return 1
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    sys.exit(main())
