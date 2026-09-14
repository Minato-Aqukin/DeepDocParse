"""Explicit application boundaries; adapters own storage and transport semantics."""

from typing import Protocol


class ApplicationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CorpusStore(Protocol):
    def version(self, version_id: str) -> dict: ...
    def versions(self) -> list[dict]: ...
    def evidence(self, evidence_id: str) -> dict: ...
    def publish_parse(
        self,
        task: dict,
        *,
        layout_key: str,
        bundle_key: str,
        records: list[dict],
        provider: dict,
        degraded: list[str],
    ) -> bool: ...


class BlobStore(Protocol):
    def read(self, key: str, maximum: int) -> bytes: ...
    def write(self, content: bytes) -> str: ...
    def path(self, key: str) -> str: ...


class SearchIndex(Protocol):
    def keyword_search(self, query: str, version_ids: list[str], limit: int) -> list[dict]: ...


class TaskStore(Protocol):
    def claim(self) -> dict | None: ...
    def renew(self, task_id: str, generation: int) -> bool: ...
    def fail(self, task: dict, code: str, message: str) -> None: ...


class ExecutionProvider(Protocol):
    async def parse(self, snapshot_path: str) -> dict: ...
    async def generate(
        self, messages: list[dict], *, execution_policy: str, allow_remote: bool, max_tokens: int = 1024
    ) -> tuple[str, dict]: ...
    def capabilities(self) -> dict: ...


class PolicyService(Protocol):
    def authorize_versions(self, version_ids: list[str]) -> list[str]: ...
