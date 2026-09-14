"""Explicit CPU/model adapters; no network at import or implicit remote fallback."""

import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ddp_core.application.ports import ApplicationError


@dataclass(frozen=True)
class ModelSelection:
    endpoint: str
    model: str
    location: str = "local"
    api_key: str | None = field(default=None, repr=False)
    provenance: dict | None = field(default=None, repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if (
            not self.model
            or self.location not in {"local", "remote"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ApplicationError(
                "invalid_provider", "select a model and an explicit provider endpoint"
            )
        if self.location == "local":
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
                raise ApplicationError(
                    "invalid_provider", "local model endpoint must use literal loopback HTTP"
                )
        elif parsed.scheme != "https":
            raise ApplicationError("invalid_provider", "remote model endpoint must use HTTPS")


class LocalExecutionProvider:
    def __init__(self, model: ModelSelection | None = None):
        self.model = model

    def capabilities(self):
        return {
            "parse": {
                "available": importlib.util.find_spec("pypdfium2") is not None,
                "provider": "borndigital",
                "execution": "cpu_subprocess",
                "requires_text_layer": True,
            },
            "retrieval": {"available": True, "mode": "keyword", "vector_available": False},
            "generation": {
                "available": self.model is not None,
                "status": "configured_unverified" if self.model else "model_unavailable",
                "model": self.model.model if self.model else None,
                "location": self.model.location if self.model else None,
            },
            "degraded": ["embedding_unavailable", "vision_unavailable"],
        }

    async def parse(self, snapshot_path: str):
        descriptor = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ddp_local.worker_parse",
                str(descriptor),
                snapshot_path.rsplit("/", 1)[-1],
                pass_fds=(descriptor,),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 120)
            except TimeoutError as exc:
                raise ApplicationError(
                    "parse_timeout", "CPU parse exceeded the 120 second budget"
                ) from exc
            if len(output) > 32 * 1024 * 1024:
                raise ApplicationError("layout_too_large", "CPU output exceeds the local budget")
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError) as exc:
                code = "out_of_memory" if process.returncode in {-9, 137} else "parse_failed"
                raise ApplicationError(code, "CPU subprocess ended without valid output") from exc
            if process.returncode or "error" in result:
                raise ApplicationError(
                    result.get("error", "parse_failed"), "CPU parser could not complete this PDF"
                )
            return result["layout"]
        finally:
            os.close(descriptor)
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def generate(self, messages, *, execution_policy, allow_remote, max_tokens=1024):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ApplicationError("wiki_budget_invalid", "completion token budget must be within 1..8192")
        if execution_policy not in {"local_only", "remote_allowed"}:
            raise ApplicationError("invalid_execution_policy", "unknown execution policy")
        selection = self.model
        if selection is None:
            raise ApplicationError(
                "model_unavailable",
                "Select and start a local instruction model to generate answers or Wiki",
            )
        if selection.location == "remote" and (
            execution_policy != "remote_allowed" or not allow_remote
        ):
            raise ApplicationError(
                "remote_execution_denied",
                "Remote generation sends the question and selected evidence; explicit "
                "selection and consent are required",
            )
        import httpx

        headers = {"Authorization": "Bearer " + selection.api_key} if selection.api_key else {}
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=120
            ) as client:
                async with client.stream(
                    "POST",
                    selection.endpoint.rstrip("/") + "/chat/completions",
                    headers=headers,
                    json={
                        "model": selection.model,
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": max_tokens,
                        "stream": False,
                    },
                ) as response:
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 1024 * 1024:
                            raise ApplicationError(
                                "model_response_too_large", "model response exceeds local budget"
                            )
                    if response.status_code != 200:
                        oom = b"out of memory" in body.lower() or b"out_of_memory" in body.lower()
                        raise ApplicationError(
                            "out_of_memory" if oom else "model_unavailable",
                            f"model provider returned HTTP {response.status_code}",
                        )
            result = json.loads(body)
            if result["choices"][0].get("finish_reason") == "length" or result.get("usage", {}).get("completion_tokens", 0) > max_tokens:
                raise ApplicationError("generation_budget_exceeded", "model exhausted or exceeded its completion budget")
            output = result["choices"][0]["message"]["content"]
            if not isinstance(output, str) or not output.strip():
                raise ValueError("empty output")
        except httpx.HTTPError as exc:
            raise ApplicationError(
                "model_unavailable", "selected model endpoint is unreachable; no fallback was used"
            ) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ApplicationError(
                "model_response_invalid", "selected model returned an invalid completion"
            ) from exc
        return output, {
            "name": selection.model,
            "engine": "openai-compatible",
            "location": selection.location,
            "endpoint": selection.endpoint,
            "provider_resolved": True,
            **(selection.provenance or {}),
        }
