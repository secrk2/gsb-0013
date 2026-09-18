"""报关通数据模型与业务状态机。

状态机规则集中在本文件，API 层只做取数 + 调 `guard_transition`，
保证「非法回退拦下来说原因」只有一处事实来源。
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ---------- 枚举 ----------

class Role(str, enum.Enum):
    BROKER = "broker"          # 本行报关员
    CUSTOMS = "customs"        # 海关审单员
    ENTERPRISE = "enterprise"  # 进出口企业管理员
    SUPERVISOR = "supervisor"  # 监管员


ROLE_LABELS = {
    Role.BROKER: "报关员",
    Role.CUSTOMS: "海关审单员",
    Role.ENTERPRISE: "企业管理员",
    Role.SUPERVISOR: "监管员",
}


class EnterpriseStatus(str, enum.Enum):
    FILING = "filing"     # 备案中
    ACTIVE = "active"     # 已备案
    REJECTED = "rejected"  # 备案驳回


class ContractStatus(str, enum.Enum):
    PENDING = "pending"    # 待签署
    ACTIVE = "active"      # 已生效
    TERMINATED = "terminated"  # 已终止


class DeclStatus(str, enum.Enum):
    ENTRUSTED = "entrusted"    # 委托中
    ENTERED = "entered"        # 已录入
    REVIEWING = "reviewing"    # 审单中
    INSPECTING = "inspecting"  # 查验中
    RELEASED = "released"      # 已放行
    CLOSED = "closed"          # 已结关
    CANCELLED = "cancelled"    # 已撤销


STATUS_LABELS = {
    DeclStatus.ENTRUSTED: "委托中",
    DeclStatus.ENTERED: "已录入",
    DeclStatus.REVIEWING: "审单中",
    DeclStatus.INSPECTING: "查验中",
    DeclStatus.RELEASED: "已放行",
    DeclStatus.CLOSED: "已结关",
    DeclStatus.CANCELLED: "已撤销",
}

# 报关单合法流转：键为当前态，值为可流转的目标态 + 业务语义
ALLOWED_TRANSITIONS: dict[DeclStatus, dict[DeclStatus, str]] = {
    DeclStatus.ENTRUSTED: {
        DeclStatus.ENTERED: "报关员完成报关单录入",
        DeclStatus.CANCELLED: "委托取消",
    },
    DeclStatus.ENTERED: {
        DeclStatus.REVIEWING: "提交海关审单",
        DeclStatus.CANCELLED: "企业撤单",
    },
    DeclStatus.REVIEWING: {
        DeclStatus.ENTERED: "审单退回，要求补录修正",
        DeclStatus.INSPECTING: "审单布控，转查验",
        DeclStatus.RELEASED: "审单审结，无查验放行",
        DeclStatus.CANCELLED: "审单撤销",
    },
    DeclStatus.INSPECTING: {
        DeclStatus.RELEASED: "查验无误，放行",
        DeclStatus.REVIEWING: "查验异常，退回重新审单",
    },
    DeclStatus.RELEASED: {
        DeclStatus.CLOSED: "办结结关手续",
    },
    DeclStatus.CLOSED: {},
    DeclStatus.CANCELLED: {},
}

# 终态：终态再做任何流转都拦下
TERMINAL_STATUSES = {DeclStatus.CLOSED, DeclStatus.CANCELLED}

# 各角色在一次流转中的权限（职责分离，越权操作同样拦下并说明原因）
TRANSITION_ROLES: dict[tuple[DeclStatus, DeclStatus], set[Role]] = {
    (DeclStatus.ENTRUSTED, DeclStatus.ENTERED): {Role.BROKER},
    (DeclStatus.ENTRUSTED, DeclStatus.CANCELLED): {Role.ENTERPRISE, Role.BROKER},
    (DeclStatus.ENTERED, DeclStatus.REVIEWING): {Role.BROKER},
    (DeclStatus.ENTERED, DeclStatus.CANCELLED): {Role.ENTERPRISE, Role.BROKER},
    (DeclStatus.REVIEWING, DeclStatus.ENTERED): {Role.CUSTOMS},
    (DeclStatus.REVIEWING, DeclStatus.INSPECTING): {Role.CUSTOMS},
    (DeclStatus.REVIEWING, DeclStatus.RELEASED): {Role.CUSTOMS},
    (DeclStatus.REVIEWING, DeclStatus.CANCELLED): {Role.CUSTOMS},
    (DeclStatus.INSPECTING, DeclStatus.RELEASED): {Role.CUSTOMS},
    (DeclStatus.INSPECTING, DeclStatus.REVIEWING): {Role.CUSTOMS},
    (DeclStatus.RELEASED, DeclStatus.CLOSED): {Role.CUSTOMS, Role.BROKER},
}


class BizError(Exception):
    """业务规则被拦下时抛出，message 即给用户看的「原因」。"""

    def __init__(self, reason: str, code: str = "biz_rule_violation", status_code: int = 409):
        self.reason = reason
        self.code = code
        self.status_code = status_code
        super().__init__(reason)


def guard_transition(current: DeclStatus, target: DeclStatus, role: Role) -> None:
    """校验状态流转是否合法、角色是否有权；不合法一律抛 BizError 并说明原因。"""
    if current == target:
        raise BizError(f"报关单当前已是「{STATUS_LABELS[current]}」，无需重复流转（幂等拦截）",
                       code="same_status", status_code=409)
    if current in TERMINAL_STATUSES:
        raise BizError(
            f"报关单已处于终态「{STATUS_LABELS[current]}」，不能再变更为「{STATUS_LABELS[target]}」。"
            f"终态单据只可查看，不可回退。",
            code="terminal_status",
        )
    nxt = ALLOWED_TRANSITIONS.get(current, {})
    if target not in nxt:
        allowed = "、".join(f"「{STATUS_LABELS[s]}」" for s in nxt) or "无（终态）"
        raise BizError(
            f"非法流转：不允许从「{STATUS_LABELS[current]}」直接变为「{STATUS_LABELS[target]}」。"
            f"当前状态允许的下一步是：{allowed}。如需退回请走合规退单通道，不能直接回退。",
            code="illegal_transition",
        )
    allowed_roles = TRANSITION_ROLES.get((current, target), set())
    if role not in allowed_roles:
        raise BizError(
            f"当前账号角色无权执行「{STATUS_LABELS[current]} → {STATUS_LABELS[target]}」，"
            f"该操作需：{'、'.join(ROLE_LABELS[r] for r in allowed_roles)}。",
            code="role_forbidden",
            status_code=403,
        )


# ---------- ORM 模型 ----------

class Enterprise(Base):
    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)   # 海关注册编码
    name_full: Mapped[str] = mapped_column(String(200))          # 企业全称（脱敏保护对象）
    name_short: Mapped[str] = mapped_column(String(64))          # 缩写+编号，默认展示名
    credit_code: Mapped[str] = mapped_column(String(32))
    contact_person: Mapped[str] = mapped_column(String(64), default="")
    contact_phone: Mapped[str] = mapped_column(String(32), default="")
    ie_flag: Mapped[str] = mapped_column(String(16), default="进出口")
    status: Mapped[EnterpriseStatus] = mapped_column(Enum(EnterpriseStatus), default=EnterpriseStatus.FILING)
    filed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="enterprise")
    contracts: Mapped[list["Contract"]] = relationship(back_populates="enterprise")
    declarations: Mapped[list["Declaration"]] = relationship(back_populates="enterprise")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[Role] = mapped_column(Enum(Role))
    enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id"), nullable=True)
    port: Mapped[str] = mapped_column(String(64), default="")  # 报关员常驻口岸
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    enterprise: Mapped[Enterprise | None] = relationship(back_populates="users")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Contract(Base):
    """委托合同：企业备案通过后签署，生效后报关单才能立项。"""
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"))
    status: Mapped[ContractStatus] = mapped_column(Enum(ContractStatus), default=ContractStatus.PENDING)
    scope_text: Mapped[str] = mapped_column(Text, default="进出口货物报关申报全流程委托")
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    enterprise: Mapped[Enterprise] = relationship(back_populates="contracts")


class Declaration(Base):
    __tablename__ = "declarations"

    id: Mapped[int] = mapped_column(primary_key=True)
    decl_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # 报关单编号
    client_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)  # 客户端幂等引用
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"))
    broker_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # 派单的报关员
    customs_officer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    status: Mapped[DeclStatus] = mapped_column(Enum(DeclStatus), default=DeclStatus.ENTRUSTED, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 乐观版本号，离线恢复合并依据
    ie_type: Mapped[str] = mapped_column(String(8), default="import")  # import/export
    port: Mapped[str] = mapped_column(String(64), default="")
    cargo_name: Mapped[str] = mapped_column(String(200), default="")
    hs_code: Mapped[str] = mapped_column(String(20), default="")
    qty: Mapped[str] = mapped_column(String(40), default="")
    total_value: Mapped[float] = mapped_column(Numeric(16, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")

    tax_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    tax_due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tax_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    offline_created: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[str] = mapped_column(Text, default="")

    entrust_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enterprise: Mapped[Enterprise] = relationship(back_populates="declarations")
    broker: Mapped[User | None] = relationship(foreign_keys=[broker_id])
    events: Mapped[list["DeclarationEvent"]] = relationship(
        back_populates="declaration", cascade="all, delete-orphan", order_by="DeclarationEvent.id")
    inspections: Mapped[list["Inspection"]] = relationship(
        back_populates="declaration", cascade="all, delete-orphan", order_by="Inspection.scheduled_at")


class DeclarationEvent(Base):
    """报关单全生命周期事件流（状态机留痕）。"""
    __tablename__ = "declaration_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    from_status: Mapped[DeclStatus | None] = mapped_column(Enum(DeclStatus), nullable=True)
    to_status: Mapped[DeclStatus] = mapped_column(Enum(DeclStatus))
    note: Mapped[str] = mapped_column(String(400), default="")
    is_offline: Mapped[bool] = mapped_column(Boolean, default=False)  # 口岸离线期间操作，恢复后补传
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    declaration: Mapped[Declaration] = relationship(back_populates="events")


class Inspection(Base):
    """查验排期。"""
    __tablename__ = "inspections"

    id: Mapped[int] = mapped_column(primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    port: Mapped[str] = mapped_column(String(64))
    bay: Mapped[str] = mapped_column(String(64), default="")
    inspector_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/inspecting/done/abnormal
    result_note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    declaration: Mapped[Declaration] = relationship(back_populates="inspections")


class NameReveal(Base):
    """报关员二次确认查看企业全称的留痕（谁、何时、哪家企业、填的理由）。"""
    __tablename__ = "name_reveals"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    broker_name: Mapped[str] = mapped_column(String(64))
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"))
    enterprise_name: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IdempotencyRecord(Base):
    """Idempotency-Key 幂等记录：同一把钥匙的重试/离线重放返回首响应，绝不产生重复单据。"""
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_idem_user_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), index=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    response_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    replayed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
