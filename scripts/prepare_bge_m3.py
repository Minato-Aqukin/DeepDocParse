"""把 bge-m3 的 pytorch_model.bin 转成 safetensors —— TEI 容器只认后者。

背景：HF/ModelScope 上 BAAI/bge-m3 只提供 .bin（无 safetensors），而
text-embeddings-inference 拒绝加载 .bin，故需本地转一次。权重逐张等价拷贝，不改数值。

前置：pip install torch safetensors
用法：python scripts/prepare_bge_m3.py [模型目录]
      默认目录 ../models/bge-m3（刻意放在仓库外，2GB+ 不入库）
"""
import hashlib
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "bge-m3"
# 取自 HF 的 X-Linked-Size / X-Linked-ETag（后者就是 sha256）。
# 校验用哈希而非只看大小：多来源续传（HF/ModelScope 混用 curl -C -）可能拼出
# 大小对但内容错的文件，只有哈希能发现。
EXPECTED_BIN_SIZE = 2271145830
EXPECTED_BIN_SHA256 = "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    model_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    src = model_dir / "pytorch_model.bin"
    dst = model_dir / "model.safetensors"

    if not src.exists():
        print(f"ERROR: 找不到 {src}", file=sys.stderr)
        return 1
    size = src.stat().st_size
    if size != EXPECTED_BIN_SIZE:
        print(f"ERROR: {src.name} 大小 {size}，期望 {EXPECTED_BIN_SIZE} —— 下载未完成。\n"
              f"       断点续传（务必只用一个来源，混用会拼出坏文件）：\n"
              f"         curl -L --ssl-no-revoke -C - -o '{src}' \\\n"
              f"           https://huggingface.co/BAAI/bge-m3/resolve/main/pytorch_model.bin",
              file=sys.stderr)
        return 1
    print(f"校验 sha256（{size / 2**30:.2f} GB）...")
    digest = sha256_of(src)
    if digest != EXPECTED_BIN_SHA256:
        print(f"ERROR: sha256 不匹配——文件已损坏，删除后重新单源下载。\n"
              f"       实际 {digest}\n       期望 {EXPECTED_BIN_SHA256}", file=sys.stderr)
        return 1
    print("sha256 校验通过")

    import torch
    from safetensors.torch import save_file

    print(f"loading {src} ({size / 2**30:.2f} GB) ...")
    state = torch.load(src, map_location="cpu", weights_only=True)
    tensors = {k: v.clone().contiguous() for k, v in state.items() if isinstance(v, torch.Tensor)}
    skipped = [k for k, v in state.items() if not isinstance(v, torch.Tensor)]

    save_file(tensors, str(dst))
    print(f"wrote {dst} —— {len(tensors)} 张权重")
    if skipped:
        print(f"跳过非张量条目：{skipped}")
    print("\nTEI 启动（dev，CPU）：\n"
          f"  docker run -d --name tei -p 18080:8080 -v '{model_dir}':/data/bge-m3 \\\n"
          "    ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3 \\\n"
          "    --model-id /data/bge-m3 --port 8080")
    return 0


if __name__ == "__main__":
    sys.exit(main())
