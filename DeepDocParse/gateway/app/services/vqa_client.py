"""VQA 运行时客户端（OpenAI 协议）。

chat.py 的流式透传直接用 httpx 反代；本模块给 mcp_server / ask_document
提供非流式的便捷调用：图片(url/dataURI) + prompt -> 文本答案。
"""
import httpx


class VQAClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def ask(self, endpoint: str, model: str, image_url: str, question: str) -> str:
        """POST {endpoint}/v1/chat/completions，返回首个 choice 的文本。

        image_url 可以是 http(s) URL 或 data:image/...;base64 URI（OpenAI 协议两者皆可）。
        """
        resp = await self._http.post(
            f"{endpoint}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": question},
                    ],
                }],
                "stream": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
