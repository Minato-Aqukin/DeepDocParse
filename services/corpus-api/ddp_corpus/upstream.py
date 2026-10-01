"""模型上游：embedding／回答走 OpenAI 兼容接口，视觉核对走网关能力契约。

embedding 与回答可通过各自配置直连 TEI／vLLM／其他兼容服务。
视觉核对独立访问网关 `/v1/models`，按 `vision` 与专用抄写提示选模，
再请求网关 `/v1/chat/completions`；不能拿回答模型的默认路由代替视觉能力。
解析任务仍由 service_client 使用解析契约处理。
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


async def embed_batched(http: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """按运行时单请求上限分批，按原查询顺序拼接向量。"""
    vectors = []
    size = settings.embedding_batch_size
    for start in range(0, len(texts), size):
        vectors.extend(await embed_texts(http, texts[start:start + size]))
    return vectors


async def embed_one(http: httpx.AsyncClient, text: str) -> list[float]:
    return (await embed_texts(http, [text]))[0]


def chat_request(http: httpx.AsyncClient, messages: list[dict], *, stream: bool,
                 response_format: dict | None = None, temperature: float | None = None,
                 max_tokens: int | None = None):
    """构造 chat 请求（不发送）——调用方决定流式消费还是一次读完。

    读超时单独配：CPU 上跑的视觉模型出第一个 token 可能要几分钟，
    用客户端默认的 300s 会在生成中途把连接掐断。
    """
    payload: dict = {"messages": messages, "stream": stream}
    if settings.chat_model:
        payload["model"] = settings.chat_model
    if response_format is not None:
        payload["response_format"] = response_format
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return http.build_request("POST", settings.chat_endpoint, json=payload,
                              headers=_headers(settings.chat_token),
                              timeout=httpx.Timeout(30.0, read=settings.chat_read_timeout))


async def select_transcription_model(http: httpx.AsyncClient, *,
                                     timeout: float | None = None) -> dict:
    """按网关清单的 vision 能力选核对模型，不借用回答通道的能力声明。"""
    headers = _headers(settings.service_token)
    response = await http.get(f"{settings.service_url}/v1/models", headers=headers,
                              timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout)
    response.raise_for_status()
    models = [item for item in response.json()["data"]
              if "vision" in item.get("capabilities", [])]
    if not models:
        raise UpstreamError(503, "no vision model registered")
    return next((item for item in models if item.get("default")), models[0])


async def transcribe_image(http: httpx.AsyncClient, image_uri: str) -> str:
    """使用独立视觉模型及其注册表抄写提示核对裁图。"""
    model = await select_transcription_model(http)
    prompt = model["transcribe_prompt"]
    headers = _headers(settings.service_token)
    response = await http.post(
        f"{settings.service_url}/v1/chat/completions", headers=headers,
        json={"model": model["id"], "stream": False, "temperature": 0,
              "messages": [{"role": "user", "content": [
                  {"type": "image_url", "image_url": {"url": image_uri}},
                  {"type": "text", "text": prompt},
              ]}]},
        timeout=httpx.Timeout(30.0, read=settings.chat_read_timeout))
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"] or ""
