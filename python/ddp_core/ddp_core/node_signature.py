"""Ed25519 验签的唯一 Python 实现（DDP-NODE-CREDENTIAL）。

**只验签，不签名**：节点私钥只有控制面一个所有者（不变式 5），语料服务
永远拿不到种子，所以这里刻意没有 sign 函数。

依赖 `cryptography`，由 `ddp-core[federation]` 声明。缺依赖时**抛错而不是
返回 False**：返回 False 会让所有凭证都显示成"签名无效"，把部署问题伪装成
对端伪造 —— 那正是本项目最怕的静默降级。
"""
from __future__ import annotations


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ModuleNotFoundError as exc:            # pragma: no cover - 部署缺依赖
        raise RuntimeError(
            "Ed25519 verification needs the `cryptography` package "
            "(install ddp-core[federation])") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True
