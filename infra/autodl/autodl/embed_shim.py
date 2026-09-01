"""bge-m3 的 CPU /v1/embeddings 垫片 —— 给没有 TEI 的机器用。

注册表的 models.autodl.yaml 里早就写了这条出路（"用 sentence-transformers
包一个 /v1/embeddings"）。这里不引 sentence-transformers，直接用 vLLM venv
里已有的 transformers + torch：bge-m3 的 dense 向量就是 CLS 池化后 L2 归一化。

**这是 TEI 的替身，不是它的等价物**：CPU 上跑、没有批处理优化、没有 sparse/colbert
那两路输出。用途只有一个 —— 让没有 TEI 的机器也能把「上传 → 索引 → 检索 → 问答」
这条产品路径走完。真机质量数字仍然要在有 TEI 的部署上量。
"""
import os

import torch
import torch.nn.functional as F
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

MODEL_DIR = os.environ.get("EMBED_MODEL_DIR") or "/root/autodl-tmp/ddp/models/bge-m3"
SERVED = "bge-m3"

tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
model = AutoModel.from_pretrained(MODEL_DIR, torch_dtype=torch.float32).eval()
app = FastAPI()


class EmbedIn(BaseModel):
    input: str | list[str]
    model: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/embeddings")
def embeddings(body: EmbedIn) -> dict:
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    with torch.no_grad():
        batch = tokenizer(texts, padding=True, truncation=True,
                          max_length=8192, return_tensors="pt")
        # bge-m3 的 dense 向量 = 最后一层 CLS，再 L2 归一化（与 FlagEmbedding 一致）
        dense = model(**batch).last_hidden_state[:, 0]
        dense = F.normalize(dense, p=2, dim=-1)
    return {
        "object": "list",
        "model": body.model or SERVED,
        "data": [{"object": "embedding", "index": i, "embedding": vec.tolist()}
                 for i, vec in enumerate(dense)],
        "usage": {"prompt_tokens": int(batch["attention_mask"].sum()), "total_tokens": 0},
    }
