"""Text agreement used by visual QA, extraction, and threshold calibration."""
import difflib
import re

TRANSCRIBE_PROMPT = (
    "把这张图里的文字**原样**抄写出来，保持原有顺序。"
    "不要翻译、不要总结、不要解释，只输出文字本身。"
)
MIN_TRANSCRIPT_CHARS = 10


def comparable(text: str) -> str:
    """Ignore punctuation and spacing, not text order or substantive characters."""
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE)


def similarity(left: str, right: str) -> float:
    # Frequent characters are meaningful in Chinese, not SequenceMatcher junk.
    return difflib.SequenceMatcher(
        None, comparable(left), comparable(right), autojunk=False).ratio()


def transcript_agrees(transcript: str, source: str, *, threshold: float) -> bool | None:
    if len(comparable(transcript)) < MIN_TRANSCRIPT_CHARS or not comparable(source):
        return None
    return similarity(transcript, source) >= threshold
