"""VQA 运行时客户端（OpenAI 协议）。

chat.py 的流式透传直接用 httpx 反代即可；本模块给 mcp_server / ask_document
提供非流式的便捷调用：图片(bytes/url) + prompt -> 文本答案。
"""
import httpx


class VQAClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def ask(self, endpoint: str, model: str, image_url: str, question: str) -> str:
        """TODO(M2): POST {endpoint}/v1/chat/completions
        body = {model, messages:[{role:"user", content:[
            {type:"image_url", image_url:{url: image_url}},
            {type:"text", text: question}]}]}
        返回 choices[0].message.content
        """
        raise NotImplementedError
