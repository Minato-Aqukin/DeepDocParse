"""DDP-Agent v1：检索判定、候选门控、断言与统一核对

Revision ID: 0011
Revises: 0010
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("message_id", sa.String(32),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("need_retrieval", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("decision_reason", sa.String(64), nullable=False,
                  server_default="legacy_message"),
        sa.Column("inherited_evidence_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("degraded", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", name="uq_agent_turns_message_id"),
    )
    op.create_index("ix_agent_turns_message_id", "agent_turns", ["message_id"], unique=True)
    op.create_table(
        "assertions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("message_id", sa.String(32),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("unsupported", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_state", sa.String(16), nullable=False,
                  server_default="unverified"),
        sa.Column("verification_mode", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "position", name="uq_assertions_message_position"),
    )
    op.create_index("ix_assertions_message_id", "assertions", ["message_id"])
    op.create_index("ix_assertions_unsupported", "assertions", ["unsupported"])
    op.create_index("ix_assertions_verification_state", "assertions", ["verification_state"])
    op.create_table(
        "retrieval_candidates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("turn_id", sa.String(32),
                  sa.ForeignKey("agent_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", sa.String(32), sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("turn_id", "rank", name="uq_retrieval_candidates_turn_rank"),
    )
    op.create_index("ix_retrieval_candidates_turn_id", "retrieval_candidates", ["turn_id"])
    op.create_index("ix_retrieval_candidates_evidence_id", "retrieval_candidates",
                    ["evidence_id"])
    op.create_index("ix_retrieval_candidates_document_id", "retrieval_candidates",
                    ["document_id"])
    op.create_index("ix_retrieval_candidates_accepted", "retrieval_candidates", ["accepted"])
    op.create_table(
        "evidence_verifications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("evidence_id", sa.String(32), sa.ForeignKey("evidence.id"), nullable=False),
        sa.Column("assertion_id", sa.String(32),
                  sa.ForeignKey("assertions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    for name, column in (
        ("ix_evidence_verifications_evidence_id", "evidence_id"),
        ("ix_evidence_verifications_assertion_id", "assertion_id"),
        ("ix_evidence_verifications_reviewer_id", "reviewer_id"),
        ("ix_evidence_verifications_created_at", "created_at"),
    ):
        op.create_index(name, "evidence_verifications", [column])

    # 历史 assistant message 变成单断言；已有 message Citation 改挂断言。
    conn = op.get_bind()
    messages = conn.execute(sa.text(
        "SELECT id, content, verified, created_at FROM messages "
        "WHERE role='assistant' ORDER BY created_at, id")).fetchall()
    for message_id, content, verified, created_at in messages:
        assertion_id = uuid.uuid4().hex
        turn_id = uuid.uuid4().hex
        cited = bool(conn.execute(sa.text(
            "SELECT 1 FROM citations WHERE source_kind='message' AND source_id=:id LIMIT 1"),
            {"id": message_id}).first())
        conn.execute(sa.text(
            "INSERT INTO agent_turns "
            "(id,message_id,need_retrieval,decision_reason,inherited_evidence_ids,created_at) "
            "VALUES (:id,:message,TRUE,'legacy_message','[]',:created)"),
            {"id": turn_id, "message": message_id, "created": created_at})
        conn.execute(sa.text(
            "INSERT INTO assertions "
            "(id,message_id,position,text,unsupported,verification_state,"
            "verification_mode,created_at) VALUES "
            "(:id,:message,0,:text,:unsupported,:state,:mode,:created)"),
            {"id": assertion_id, "message": message_id, "text": content or "",
             "unsupported": not cited,
             # 老 message.verified 只说明当时做过视觉核对；没有 Citation 时
             # 根本不知道核对的是哪条证据，不能把无证据断言迁成 passed。
             "state": "passed" if verified and cited else "unverified",
             "mode": "auto" if verified and cited else None,
             "created": created_at})
        conn.execute(sa.text(
            "UPDATE citations SET source_kind='assertion', source_id=:assertion "
            "WHERE source_kind='message' AND source_id=:message"),
            {"assertion": assertion_id, "message": message_id})


def downgrade() -> None:
    conn = op.get_bind()
    # 0010 没有地方安放判定、拒绝候选、断言级核对与人工审计。只允许回退
    # upgrade() 刚从 legacy Message 机械回填出的形状；阶段 6 一旦写入新数据，
    # 静默 drop 就是不可恢复的数据损坏，必须让操作者先备份/导出并显式处理。
    verification_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM evidence_verifications")).scalar() or 0
    candidate_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM retrieval_candidates")).scalar() or 0
    turns = conn.execute(sa.text(
        "SELECT need_retrieval, decision_reason, inherited_evidence_ids, degraded "
        "FROM agent_turns")).fetchall()
    nonlegacy_turns = sum(
        not need or reason != "legacy_message" or bool(inherited) or degraded is not None
        for need, reason, inherited, degraded in turns)
    nonlegacy_assertions = conn.execute(sa.text(
        "SELECT COUNT(*) FROM ("
        "SELECT a.message_id FROM assertions a JOIN messages m ON m.id=a.message_id "
        "GROUP BY a.message_id, m.content "
        "HAVING COUNT(*) <> 1 OR MIN(a.position) <> 0 OR MIN(a.text) <> m.content"
        ") AS changed_assertions")).scalar() or 0
    if any((verification_count, candidate_count, nonlegacy_turns, nonlegacy_assertions)):
        raise RuntimeError(
            "0011 cannot downgrade after DDP-Agent data was created: "
            f"verifications={verification_count}, candidates={candidate_count}, "
            f"agent_turns={nonlegacy_turns}, assertions={nonlegacy_assertions}. "
            "Back up/export the audit trail and revert application code without dropping "
            "these tables; automatic downgrade would destroy evidence history.")

    # 多断言可引用同一 evidence；降回 message 前只保留同 message/evidence/role 的首条，
    # 否则旧唯一约束会撞。Message.content 已保留完整投影，文本不会丢。
    rows = conn.execute(sa.text(
        "SELECT c.id, a.message_id, c.evidence_id, c.role FROM citations c "
        "JOIN assertions a ON a.id=c.source_id WHERE c.source_kind='assertion' "
        "ORDER BY c.created_at, c.id")).fetchall()
    seen: set[tuple[str, str, str]] = set()
    for citation_id, message_id, evidence_id, role in rows:
        key = (message_id, evidence_id, role)
        if key in seen:
            conn.execute(sa.text("DELETE FROM citations WHERE id=:id"), {"id": citation_id})
        else:
            seen.add(key)
            conn.execute(sa.text(
                "UPDATE citations SET source_kind='message', source_id=:message WHERE id=:id"),
                {"message": message_id, "id": citation_id})

    op.drop_table("evidence_verifications")
    op.drop_table("retrieval_candidates")
    op.drop_table("assertions")
    op.drop_table("agent_turns")
