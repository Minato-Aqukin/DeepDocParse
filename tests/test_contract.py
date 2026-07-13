"""契约测试 —— 两个用途：
1. 对内：固化 gateway 对 openapi.yaml 的实现（backend 联调的依据）
2. 对上游：mineru / deepseek-ocr.rs 升级版本前必须先跑绿，才允许换镜像版本

运行：pytest tests/ （需要 dev compose 已启动，或用 respx mock 上游）
"""
import pytest

# TODO(M1): 样例文件放 tests/fixtures/（含扫描件/表格/公式的小 PDF）


@pytest.mark.skip(reason="TODO(M1)")
async def test_parse_lifecycle():
    """提交 -> 202 + task_id -> 轮询至 succeeded -> result 含 markdown/layout_json/images。
    断言 layout_json 中块带页码与 bbox（ask_document 与 v2 索引依赖此结构）。"""


@pytest.mark.skip(reason="TODO(M1)")
async def test_parse_queue_backpressure():
    """queue_depth 达到 PARSE_QUEUE_MAX 时返回 429。"""


@pytest.mark.skip(reason="TODO(M1)")
async def test_auth_required():
    """无/错 service token -> 401。"""


@pytest.mark.skip(reason="TODO(M2)")
async def test_chat_completions_openai_compat():
    """image_url + text 的标准 OpenAI 请求可用；流式 SSE 正常；未知 model -> 404。"""


@pytest.mark.skip(reason="TODO(M3)")
async def test_ask_document_retry_pattern():
    """未解析的大文档：首次调用返回'解析中'提示；解析完成后再调用返回带出处答案。"""
