"""Calibration negatives must be different text, not merely different block IDs."""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/calibrate_verify_threshold.py"
_SPEC = importlib.util.spec_from_file_location("calibrate_verify_threshold", _SCRIPT)
calibration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(calibration)


def test_same_text_headers_are_excluded_but_different_numbers_remain_negatives():
    texts = [
        "Controller reset delay 17 milliseconds.",
        "Controller reset delay 17 milliseconds!",
        "Controller reset delay 23 milliseconds.",
        "Bluetooth low energy radio is available.",
    ]
    matched, mismatched, excluded = calibration.score_pairs(texts, texts)
    assert matched == [1.0, 1.0, 1.0, 1.0]
    assert excluded == 2
    assert len(mismatched) == 10
    assert 0.9 < max(mismatched) < 1.0  # Similar but wrong numbers are genuine hard negatives.


def test_unavailable_transcription_and_all_identical_sources_cannot_calibrate():
    text = "Repeated vendor datasheet header"
    matched, mismatched, excluded = calibration.score_pairs([text, text], [text, None])
    assert matched == [1.0]
    assert mismatched == []
    assert excluded == 1


def test_calibration_refuses_a_text_only_model_instead_of_using_another_models_prompt(tmp_path):
    registry = tmp_path / "models.yaml"
    registry.write_text("""vqa_models:
  text:
    endpoint: http://text.invalid
    default: true
    capabilities: [instruct]
  ocr:
    endpoint: http://ocr.invalid
    capabilities: [vision, no_instruct]
    options: {transcribe_prompt: 'Free OCR.'}
""")
    with pytest.raises(ValueError, match="not a registered vision model"):
        calibration.transcribe_prompt_for(str(registry), "text")
