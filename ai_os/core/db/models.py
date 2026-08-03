"""Async SQLAlchemy 2.0 persistence models (doc 13: DB Schema & Models).

Mirrors the ER diagram in `docs/13_DB_SCHEMA_AND_MODELS.md` exactly:

    EPIC ||--o{ TASK : contains
    TASK ||--o{ LOCK_AUDIT : generates
    TASK ||--o{ TOKEN_COST : incurs
    GRAPH_NODE ||--o{ GRAPH_EDGE : source/target (by fqn, not a declared FK — see
        GraphEdgeModel below for why source_fqn/target_fqn are plain indexed strings)

These are the SQLAlchemy ORM models used for on-disk persistence. They are a
deliberately separate representation from the pydantic `TaskNode`/`EpicNode` in
`ai_os/core/models.py` (the DAG Planner / Lock Manager's in-memory planning
contract) — see the docstring there for the rationale.
"""
from __future__ import annotations

import datetime
from typing import Any, List, Optional

from sqlalchemy import Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class EpicModel(Base):
    __tablename__ = "epics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # PLAN_REVIEW, RUNNING, COMPLETED, FAILED, PAUSED
    status: Mapped[str] = mapped_column(String(32), default="PLAN_REVIEW")
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[List["TaskModel"]] = relationship(
        "TaskModel", back_populates="epic", cascade="all, delete-orphan"
    )


class TaskModel(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. TASK-101
    epic_id: Mapped[str] = mapped_column(ForeignKey("epics.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # LOW, MEDIUM, HIGH, CRITICAL
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW")
    assigned_model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # PENDING, READY, RUNNING, COMPLETED, FAILED, HITL_REQUIRED
    status: Mapped[str] = mapped_column(String(32), default="PENDING")

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)

    # `default=list` is a callable (each row gets its own fresh list), not a
    # shared mutable literal — see doc 13's blueprint gap this fixes.
    target_files: Mapped[Any] = mapped_column(JSON, default=list)
    read_set: Mapped[Any] = mapped_column(JSON, default=list)
    write_set: Mapped[Any] = mapped_column(JSON, default=list)
    dependencies: Mapped[Any] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    epic: Mapped["EpicModel"] = relationship("EpicModel", back_populates="tasks")
    lock_audits: Mapped[List["LockAuditModel"]] = relationship(
        "LockAuditModel", back_populates="task", cascade="all, delete-orphan"
    )
    token_costs: Mapped[List["TokenCostModel"]] = relationship(
        "TokenCostModel", back_populates="task", cascade="all, delete-orphan"
    )


class LockAuditModel(Base):
    __tablename__ = "lock_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    filepath: Mapped[str] = mapped_column(String(512), nullable=False)
    lock_type: Mapped[str] = mapped_column(String(16), nullable=False)  # READ, WRITE
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # ACQUIRE, RELEASE
    timestamp: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

    task: Mapped["TaskModel"] = relationship("TaskModel", back_populates="lock_audits")


class TokenCostModel(Base):
    __tablename__ = "token_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # anthropic, openai, gemini, local
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)  # claude-3-5-sonnet
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usd_cost: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

    task: Mapped["TaskModel"] = relationship("TaskModel", back_populates="token_costs")


class GraphNodeModel(Base):
    __tablename__ = "graph_nodes"

    fqn: Mapped[str] = mapped_column(String(512), primary_key=True)  # e.g. src/user.ts::User
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)  # FileNode, ClassNode, FunctionNode, TypeNode
    filepath: Mapped[str] = mapped_column(String(512), nullable=False)
    stub_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_updated: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class GraphEdgeModel(Base):
    __tablename__ = "graph_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Not declared as ForeignKey(graph_nodes.fqn): the Knowledge Graph is
    # rebuilt incrementally by the Polyglot Analyzer (doc 03/08) and edges may
    # be written slightly out of order relative to their endpoint nodes during
    # a re-parse; enforcing FK integrity here would fight that pipeline. This
    # mirrors doc 13's own ER diagram, which draws the FK conceptually but
    # doesn't include source_fqn/target_fqn in GRAPH_NODE's own FK list.
    source_fqn: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    target_fqn: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)  # CONTAINS, IMPORTS, CALLS, USES_TYPE
