"""报关通数据模型与业务状态机。

状态机规则集中在本文件，API 层只做取数 + 调 `guard_transition`，
保证「非法回退拦下来说原因」只有一处事实来源。
"""
from __future__ import annotations

import enum
from datetime import datetime, timedelta

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
    INSPECTOR = "inspector"    # 监管场站查验员
    ENTERPRISE = "enterprise"  # 进出口企业管理员
    SUPERVISOR = "supervisor"  # 监管员


ROLE_LABELS = {
    Role.BROKER: "报关员",
    Role.CUSTOMS: "海关审单员",
    Role.INSPECTOR: "查验员",
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

# ---------- 查验资质 ----------
# 按 HS 编码章节（前 2 位）推导货物所需查验资质；None 表示普通货物，任意查验员/普货台位均可
QUAL_CHILLED = "chilled"   # 冷链（动植食，需冷链查验资质 + 冷链台位）
QUAL_DG = "dg"             # 危险品（危化品查验资质 + 危化台位）
QUAL_LARGE = "large"       # 大型设备（大型设备台位）

QUAL_LABELS = {
    QUAL_CHILLED: "冷链查验",
    QUAL_DG: "危险品查验",
    QUAL_LARGE: "大型设备查验",
}

# HS 章节 → 所需资质（与种子数据中的货物品类对应）
HS_CHAPTER_QUAL: dict[str, str] = {
    "02": QUAL_CHILLED, "03": QUAL_CHILLED, "16": QUAL_CHILLED,
    "28": QUAL_DG, "29": QUAL_DG,
}


def qual_for_cargo(hs_code: str, cargo_name: str = "") -> str | None:
    """按 HS 章节推导货物所需查验资质；名称含冷链/危化关键词的兜底识别。"""
    hs = (hs_code or "").strip()
    if len(hs) >= 2 and hs[:2] in HS_CHAPTER_QUAL:
        return HS_CHAPTER_QUAL[hs[:2]]
    name = cargo_name or ""
    if any(k in name for k in ("冻", "冷", "生鲜", "冷藏")):
        return QUAL_CHILLED
    if any(k in name for k in ("危化", "易燃", "易爆", "腐蚀")):
        return QUAL_DG
    if any(k in name for k in ("数控机床", "大型", "重型", "整机")):
        return QUAL_LARGE
    return None


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

    def __init__(self, reason: str, code: str = "biz_rule_violation", status_code: int = 409,
                 extra: dict | None = None):
        self.reason = reason
        self.code = code
        self.status_code = status_code
        self.extra = extra or {}  # 结构化附加信息（如逐条冲突清单），随错误体返回
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
    # 查验员：逗号分隔的资质码（chilled/dg/large）；空表示仅具普货查验资质
    qualifications: Mapped[str] = mapped_column(String(120), default="")
    # 查验员请假中：排期引擎视为不可派（与故障车位对称），原排期须走改派
    on_leave: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    enterprise: Mapped[Enterprise | None] = relationship(back_populates="users")

    @property
    def qual_list(self) -> list[str]:
        return [q.strip() for q in (self.qualifications or "").split(",") if q.strip()]


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


class Yard(Base):
    """监管场站：查验在监管场站排期，按场站维度组织车位与查验员。"""
    __tablename__ = "yards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    port: Mapped[str] = mapped_column(String(64), index=True)
    # 营业窗口（本地时间，小时浮点），拖到窗口外落位须二次确认并填原因留痕
    open_hour: Mapped[float] = mapped_column(Numeric(4, 2), default=8.5)
    close_hour: Mapped[float] = mapped_column(Numeric(4, 2), default=17.5)
    work_weekend: Mapped[bool] = mapped_column(Boolean, default=False)  # 周末是否作业
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    bays: Mapped[list["Bay"]] = relationship(
        back_populates="yard", cascade="all, delete-orphan", order_by="Bay.sort_no, Bay.id")


class Bay(Base):
    """查验车位（台位）。kind 决定能查哪类货，out_of_service 表示故障停用。"""
    __tablename__ = "bays"
    __table_args__ = (
        UniqueConstraint("yard_id", "code", name="uq_bay_yard_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    yard_id: Mapped[int] = mapped_column(ForeignKey("yards.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))           # 如 A-12
    name: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(String(16), default="general")  # general/chilled/dg/large
    sort_no: Mapped[int] = mapped_column(Integer, default=0)
    out_of_service: Mapped[bool] = mapped_column(Boolean, default=False)  # 车位故障
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    yard: Mapped[Yard] = relationship(back_populates="bays")


BAY_KIND_LABELS = {
    "general": "普货台位",
    "chilled": "冷链查验平台",
    "dg": "危化查验专区",
    "large": "大型设备台位",
}

# 货物资质 → 唯一匹配的台位类型
QUAL_TO_BAY_KIND = {
    QUAL_CHILLED: "chilled",
    QUAL_DG: "dg",
    QUAL_LARGE: "large",
}


class Inspection(Base):
    """查验排期：一条排期 = 某单在某场站某车位、由某查验员在某时段实施查验。"""
    __tablename__ = "inspections"

    id: Mapped[int] = mapped_column(primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    yard_id: Mapped[int | None] = mapped_column(ForeignKey("yards.id"), nullable=True, index=True)
    bay_id: Mapped[int | None] = mapped_column(ForeignKey("bays.id"), nullable=True, index=True)
    inspector_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)      # 计划开始
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 计划结束（重叠判定用）
    duration_minutes: Mapped[int] = mapped_column(Integer, default=120)       # 单查标准耗时
    required_qual: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 排期时按 HS 快照，防止改归类后口径漂移

    # 应查验日：布控排期时确定（通常=计划查验日），及时率「日口径」分母基准
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # pending 待查验 / inspecting 查验中 / done 已完成 / abnormal 异常 / cancelled 已取消
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    cancel_reason: Mapped[str] = mapped_column(String(300), default="")
    result_note: Mapped[str] = mapped_column(String(300), default="")

    # 链式重排批次号：同一次改派触发的后移共用一批，便于留痕与回滚展示
    chain_batch: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    reassign_count: Mapped[int] = mapped_column(Integer, default=0)  # 被改派/顺延次数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    declaration: Mapped[Declaration] = relationship(back_populates="inspections")
    yard: Mapped[Yard | None] = relationship()
    bay: Mapped[Bay | None] = relationship()
    inspector: Mapped[User | None] = relationship(foreign_keys=[inspector_id])

    @property
    def end_time(self) -> datetime:
        return self.scheduled_end or (self.scheduled_at + timedelta(minutes=self.duration_minutes or 120))


class ScheduleChange(Base):
    """排期变更留痕：安排/拖拽改期/超窗口/改派/链式顺延/取消/故障请假 全部逐条留痕。"""
    __tablename__ = "schedule_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspections.id"), nullable=True, index=True)
    declaration_id: Mapped[int | None] = mapped_column(ForeignKey("declarations.id"), nullable=True, index=True)
    yard_id: Mapped[int | None] = mapped_column(ForeignKey("yards.id"), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")

    change_type: Mapped[str] = mapped_column(String(24), index=True)
    # schedule 安排 / drag 拖拽改期 / out_of_window 超窗口落位 / reassign 改派
    # chain_shift 链式顺延 / cancel 取消 / bay_broken 车位故障 / inspector_leave 查验员请假
    reason: Mapped[str] = mapped_column(String(400), default="")
    detail: Mapped[str] = mapped_column(Text, default="")  # JSON：前后时间/车位/查验员、批次、连锁条目
    chain_batch: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
