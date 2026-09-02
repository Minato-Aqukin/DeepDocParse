#!/usr/bin/env python
"""准备 plan.md 指定的五域评测语料（不把 40MB+ 真数据提交进 git）。

四个真实域固定取自 OmniDocBench v1.6，各 10 页；代码密集域由
``scripts/make_fixtures.py`` 确定性生成 24 页。下载会校验官方标注 SHA-256，
只取 manifest 指定的 40 张图，并包装成 bitmap-only 单页 PDF 供 /v1/parse 使用。
"""
import argparse
import hashlib
import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path


EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
DEFAULT_MANIFEST = EVAL_DIR / "datasets" / "omnidocbench-v1.6-slices.json"
DEFAULT_OUTPUT = ROOT / ".eval-cache" / "omnidocbench-v1.6"
HF_IMAGES = ("https://huggingface.co/datasets/opendatalab/OmniDocBench/resolve/"
             "d386947f7fc3bafdcd756c8485845a2f43a19875/images/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(manifest: dict) -> list[str]:
    """返回按 manifest 顺序展开的页名，并钉住 4×10 与切片互斥。"""
    slices = manifest.get("slices") or {}
    if set(slices) != {"论文双栏", "公式密集", "图表引用", "扫描版老手册"}:
        raise ValueError(f"真实域必须恰好是四类，收到 {sorted(slices)}")
    for name, pages in slices.items():
        if len(pages) != 10:
            raise ValueError(f"{name} 必须固定 10 页，收到 {len(pages)}")
    flattened = [page for pages in slices.values() for page in pages]
    if len(flattened) != len(set(flattened)):
        raise ValueError("不同域复用了同一页，会污染分切片归因")
    code = ((manifest.get("synthetic") or {}).get("代码密集") or {})
    if not 20 <= int(code.get("pages", 0)) <= 30:
        raise ValueError("自建代码集必须是 20~30 页")
    if len(code.get("core_page_indices") or []) != 10:
        raise ValueError("代码密集核心切片必须固定 10 页")
    return flattened


def validate_selection(manifest: dict, by_image: dict[str, dict]) -> None:
    """按官方标注验证域名不是人工贴标签；标准写在 manifest 也钉在代码里。"""
    errors = []
    for slice_name, images in manifest["slices"].items():
        for image in images:
            entry = by_image[image]
            info = entry.get("page_info") or {}
            attributes = info.get("page_attribute") or {}
            blocks = [block for block in entry.get("layout_dets") or []
                      if not block.get("ignore")]
            categories = [block.get("category_type") for block in blocks]
            if slice_name == "论文双栏":
                ok = attributes.get("layout") == "double_column"
            elif slice_name == "公式密集":
                ok = sum(kind in {"equation_isolated", "equation_semantic"}
                         for kind in categories) >= 20
            elif slice_name == "图表引用":
                ok = categories.count("chart_mask") >= 1
            else:
                evidence = ((manifest.get("selection_evidence") or {})
                            .get("扫描版老手册") or {}).get(image)
                source_text = " ".join(str(block.get("text") or block.get("html")
                                               or block.get("latex") or "")
                                       for block in blocks)
                ok = (attributes.get("data_source") == "book"
                      and bool(evidence) and evidence in source_text)
            if not ok:
                errors.append(f"{slice_name}/{image}")
    if errors:
        raise ValueError(f"{len(errors)} 页不满足所属域的官方标注条件：{errors[:3]}")


def download(url: str, destination: Path) -> None:
    """可续跑下载：已有文件保留；新文件写 .part 后原子替换。"""
    if destination.exists():
        print(f"[已有] {destination.name}", flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "DeepDocParse-eval/1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        total = int(response.headers.get("content-length") or 0)
        done = 0
        next_report = 5 * 1024 * 1024
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            done += len(chunk)
            if done >= next_report:
                suffix = f"/{total // 1024 // 1024} MiB" if total else " MiB"
                print(f"  {destination.name}: {done // 1024 // 1024}{suffix}", flush=True)
                next_report += 5 * 1024 * 1024
    partial.replace(destination)


def image_to_pdf(image_path: Path, pdf_path: Path) -> None:
    from PIL import Image

    if pdf_path.exists():
        return
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        image.convert("RGB").save(pdf_path, "PDF", resolution=150.0)


def images_to_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    """把一个切片合成 10 页 PDF；否则每题单页会令页码命中率天然变绿。"""
    from PIL import Image

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    partial = pdf_path.with_suffix(pdf_path.suffix + ".part")
    pages = []
    try:
        for image_path in image_paths:
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                pages.append(image.convert("RGB"))
        pages[0].save(partial, "PDF", resolution=150.0, save_all=True,
                      append_images=pages[1:])
        partial.replace(pdf_path)
    finally:
        for page in pages:
            page.close()
        partial.unlink(missing_ok=True)


def prepare(manifest_path: Path, output: Path, annotation: Path | None,
            *, skip_images: bool = False) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = validate_manifest(manifest)
    source = manifest["source"]
    output.mkdir(parents=True, exist_ok=True)

    annotation_path = output / "OmniDocBench.json"
    if annotation is not None:
        if not annotation_path.exists() or annotation.resolve() != annotation_path.resolve():
            shutil.copy2(annotation, annotation_path)
    else:
        download(source["annotation_url"], annotation_path)
    actual = sha256(annotation_path)
    if actual != source["annotation_sha256"]:
        raise RuntimeError(f"官方标注校验失败：期望 {source['annotation_sha256']}，实际 {actual}")

    entries = json.loads(annotation_path.read_text(encoding="utf-8"))
    by_image = {entry.get("page_info", {}).get("image_path"): entry for entry in entries}
    missing = [image for image in selected if image not in by_image]
    if missing:
        raise RuntimeError(f"manifest 有 {len(missing)} 页不在官方标注里：{missing[:3]}")
    validate_selection(manifest, by_image)
    subset = [by_image[image] for image in selected]
    (output / "OmniDocBench.subset.json").write_text(
        json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[标注] 4 域 × 10 页，SHA-256 {actual}", flush=True)

    if skip_images:
        return
    for index, image in enumerate(selected, start=1):
        quoted = urllib.parse.quote(image, safe="/")
        image_path = output / "images" / image
        print(f"[{index:02d}/{len(selected)}] {image}", flush=True)
        download(HF_IMAGES + quoted, image_path)
        image_to_pdf(image_path, output / "documents" / f"{Path(image).stem}.pdf")
    for slice_name, images in manifest["slices"].items():
        images_to_pdf([output / "images" / image for image in images],
                      output / "slice-documents" / f"{slice_name}.pdf")
        print(f"[切片] {slice_name}: 10 页", flush=True)
    print(f"完成：{output}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--annotation", type=Path, help="已有 OmniDocBench.json；省略则下载")
    parser.add_argument("--skip-images", action="store_true", help="只校验/裁出子集标注")
    args = parser.parse_args()
    try:
        prepare(args.manifest, args.output, args.annotation, skip_images=args.skip_images)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"准备失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
