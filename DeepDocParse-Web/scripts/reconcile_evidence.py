"""双写对拍：老 JSON 里的定位元组与新表行**逐条相同**（plan.md 阶段 2b 的验收标准）。

    python scripts/reconcile_evidence.py            # 各抽 200 条
    python scripts/reconcile_evidence.py --limit 0  # 全量

退出码 0 = 一致。**读仍然走老路，所以这个脚本是阶段 3 之前唯一能发现
"新表悄悄少了一批"的手段** —— 少了的那部分，切读之后会变成"这条回答没有出处"。

## 三类差异，只有两类是问题

    老有 / 新有                一致
    老有 / 新无 / 块还在        **真问题** —— 双写漏了
    老有 / 新无 / 块没了        正常：老路径读这条同样是 resolved=False
                              （分块规则变过，或文档被删）。双写按同样的规则跳过
    老无 / 新有                **真问题** —— 新表凭空多出一条出处

第三类比第二类更值得警惕：多出来的出处会在阶段 3 读切换后**直接显示给用户**，
而它没有任何老记录背书。这正是不变式 1 说的"带着已验证标记的假出处"。
"""
import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings                                  # noqa: E402
from app.models import Chunk, ExtractionItem, Message            # noqa: E402
from ddp_core.models import Citation, Evidence                   # noqa: E402


def _locators(citations) -> set[tuple[str, int]]:
    """与 `app/evidence.py::_locator` **同一条判据** —— 判据不一致的对拍毫无意义。"""
    out = set()
    for c in citations or []:
        job, seq = c.get("parse_job_id"), c.get("seq")
        if job and seq is not None:
            out.add((job, int(seq)))
    return out


async def _new_side(session, source_kind: str, source_id: str) -> set[tuple[str, int]]:
    rows = await session.execute(
        select(Evidence.parse_job_id, Evidence.seq)
        .join(Citation, Citation.evidence_id == Evidence.id)
        .where(Citation.source_kind == source_kind, Citation.source_id == source_id,
               Citation.role == "primary"))
    return {(job, seq) for job, seq in rows}


async def _live_chunks(session, keys: set[tuple[str, int]]) -> set[tuple[str, int]]:
    if not keys:
        return set()
    rows = await session.execute(
        select(Chunk.parse_job_id, Chunk.seq)
        .where(Chunk.parse_job_id.in_({j for j, _ in keys}),
               Chunk.seq.in_({s for _, s in keys})))
    return {(j, s) for j, s in rows} & keys


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="每个平面抽多少条（0 = 全量）")
    args = ap.parse_args()

    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    missing, extra, stale, checked = [], [], 0, 0
    async with maker() as session:
        # 问答平面
        stmt = select(Message).where(Message.role == "assistant").order_by(Message.created_at.desc())
        if args.limit:
            stmt = stmt.limit(args.limit)
        for message in (await session.execute(stmt)).scalars().all():
            old = _locators(message.citations)
            if not old:
                continue
            checked += 1
            new = await _new_side(session, "message", message.id)
            live = await _live_chunks(session, old - new)
            missing += [("message", message.id, k) for k in live]
            stale += len((old - new) - live)
            extra += [("message", message.id, k) for k in new - old]

        # 抽取平面（出处是字段级的）
        stmt = select(ExtractionItem).order_by(ExtractionItem.created_at.desc())
        if args.limit:
            stmt = stmt.limit(args.limit)
        for item in (await session.execute(stmt)).scalars().all():
            for name, field in (item.fields or {}).items():
                old = _locators(field.get("citations"))
                if not old:
                    continue
                checked += 1
                source_id = f"{item.id}:{name}"
                new = await _new_side(session, "extract_field", source_id)
                live = await _live_chunks(session, old - new)
                missing += [("extract_field", source_id, k) for k in live]
                stale += len((old - new) - live)
                extra += [("extract_field", source_id, k) for k in new - old]

    await engine.dispose()

    print(f"对拍了 {checked} 条来源")
    print(f"  一致之外：漏写 {len(missing)} · 多写 {len(extra)} · 块已消失（正常）{stale}")
    for label, rows in (("漏写（块还在，双写没记）", missing), ("多写（新表凭空多出）", extra)):
        for kind, source_id, key in rows[:20]:
            print(f"  {label}: {kind} {source_id} -> {key}")
        if len(rows) > 20:
            print(f"  …还有 {len(rows) - 20} 条")

    if not checked:
        # 空跑不算通过 —— 库里没数据时这个脚本什么都没验证，别让它绿着骗人
        print("没有任何带出处的记录可对拍：这不是通过，是没验到。"
              "先跑一次问答或抽取（scripts/e2e_web.py）再来")
        return 2
    if missing or extra:
        print("对拍失败：双写与老 JSON 不一致")
        return 1
    print("对拍通过：老 JSON 与新表逐条相同")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
