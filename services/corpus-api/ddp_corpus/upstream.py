"""OpenAI 兼容上游（embedding 与 chat）。

与 service_client.py 的区别是**耦合程度**：
- service_client 走 DeepDocParse 的解析契约（openapi.yaml），那是本层不可替代的依赖
- 这里只要求"OpenAI 兼容"，缺省指向 service，但可以直连 TEI / vLLM / 任何兼容服务（ADR #17）

因此本层不绑定 DeepDocParse 的部署形态：只想用解析能力的人不必被迫接受它的 VQA 模型。
"""
import httpx

from ddp_corpus.config import settings


class UpstreamError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"upstream returned {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token or settings.service_token}"}


async def embed_texts(http: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """一次请求向量化一批文本，按 index 排序返回。

    调用方负责分批（见 settings.embedding_batch_size）：运行时对单请求条数有上限，
    整批超限会被直接拒掉而不是截断。
    """
    payload: dict = {"input": texts}
    if settings.embedding_model:
        payload["model"] = settings.embedding_model
    resp = await http.post(settings.embeddings_endpoint, json=payload,
                           headers=_headers(settings.embedding_token))
    if resp.status_code != 200:
        raise UpstreamError(resp.status_code, resp.text)
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    if len(data) != len(texts):
        raise UpstreamError(200, f"expected {len(texts)} vectors, got {len(data)}")
    return [d["embedding"] for d in data]


async def embed_one(http: httpx.AsyncClient, text: str) -> list[float]:
    return (await embed_texts(http, [text]))[0]


def chat_request(http: httpx.AsyncClient, messages: list[dict], *, stream: bool):
    """构造 chat 请求（不发送）——调用方决定流式消费还是一次读完。

    读超时单独配：CPU 上跑的视觉模型出第一个 token 可能要几分钟，
    用客户端默认的 300s 会在生成中途把连接掐断。
    """
    payload: dict = {"messages": messages, "stream": stream}
    if settings.chat_model:
        payload["model"] = settings.chat_model
    return http.build_request("POST", settings.chat_endpoint, json=payload,
                              headers=_headers(settings.chat_token),
                              timeout=httpx.Timeout(30.0, read=settings.chat_read_timeout))
