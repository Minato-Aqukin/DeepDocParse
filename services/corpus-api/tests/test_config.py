"""corpus-api 启动配置守卫：配错了启动即失败，而不是静默跑歪。"""
import pytest
from ddp_corpus.config import Settings


def test_chunk_max_chars_must_be_positive():
    """CHUNK_MAX_CHARS 非正数启动即失败：它喂给 `layout_to_chunks` 做分块预算。

    0 进到索引里才炸，报错离配错的地方隔了整个调用链 —— 启动时拦下来，
    错因直接指回配错的那一行。
    """
    with pytest.raises(Exception, match="CHUNK_MAX_CHARS"):
        Settings(chunk_max_chars=0)
    with pytest.raises(Exception, match="CHUNK_MAX_CHARS"):
        Settings(chunk_max_chars=-5)


def test_chunk_max_chars_accepts_positive():
    assert Settings(chunk_max_chars=800).chunk_max_chars == 800
    assert Settings(chunk_max_chars=1).chunk_max_chars == 1
