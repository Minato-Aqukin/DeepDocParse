"""模型清单的视觉默认值必须与抽取核对选路一致，且不泄漏运行时凭据。"""
import json

import pytest
from ddp_core.verification import TRANSCRIBE_PROMPT

from ddp_gateway.config import ModelEntry, Registry
from ddp_gateway.services.extraction import ExtractContext, _pick_chat


@pytest.mark.parametrize("vision_defaults, expected_vision", [
    ((False, True), "ocr"),
    ((False, False), "qwen-vl"),
    ((True, True), "qwen-vl"),
])
async def test_models_vision_default_matches_extraction_and_hides_credentials(
        client, app_state, vision_defaults, expected_vision):
    app_state.registry = Registry(vqa_models={
        "text": ModelEntry(endpoint="http://private-text:8000", default=True,
                           capabilities=["instruct"], options={"token": "private-token"}),
        "qwen-vl": ModelEntry(endpoint="http://private-vl:8000",
                              default=vision_defaults[0], capabilities=["vision"]),
        "ocr": ModelEntry(endpoint="http://private-ocr:8000", default=vision_defaults[1],
                          capabilities=["vision", "no_instruct"],
                          options={"transcribe_prompt": "Free OCR.", "token": "private-token"}),
    })
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    entries = {entry["id"]: entry for entry in body["data"]}
    assert entries["text"]["capabilities"] == ["instruct"]
    assert entries["qwen-vl"]["capabilities"] == ["vision"]
    assert entries["ocr"]["capabilities"] == ["vision", "no_instruct"]
    assert entries["text"]["default"] is True
    vision_defaults = [entry["id"] for entry in entries.values()
                       if "vision" in entry["capabilities"] and entry["default"]]
    ctx = ExtractContext(store=None, http=app_state.http,
                         registry=app_state.registry, doc_hash="test")
    assert vision_defaults == [expected_vision]
    assert vision_defaults[0] == _pick_chat(ctx, instruct=False)[0]
    assert "transcribe_prompt" not in entries["text"]
    assert entries["qwen-vl"]["transcribe_prompt"] == TRANSCRIBE_PROMPT
    assert entries["ocr"]["transcribe_prompt"] == "Free OCR."
    for entry in entries.values():
        assert isinstance(entry["default"], bool)
        assert set(entry) <= {"id", "object", "owned_by", "capabilities", "default", "transcribe_prompt"}
    assert "private-" not in json.dumps(body)
