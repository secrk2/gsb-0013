"""Pydantic 出入参模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field

from .models import (
    Role, DeclStatus, EnterpriseStatus, ContractStatus,
    ROLE_LABELS, STATUS_LABELS, QUAL_LABELS,
)


class LoginIn(BaseModel):
    username: str
    password: str


class EnterpriseFileIn(BaseModel):
    code: str = Field(min_length=3, max_length=32, description="海关注册编码")
    name_full: str = Field(min_length=4, max_length=200)
    credit_code: str = Field(min_length=6, max_length=32)
    contact_person: str = ""
    contact_phone: str = ""
    ie_flag: str = "进出口"


class ContractSignIn(BaseModel):
    enterprise_id: int
    scope_text: Optional[str] = None


class DeclarationCreateIn(BaseModel):
    enterprise_id: int
    contract_id: int
    ie_type: Literal["import", "export"] = "import"
    port: str = Field(min_length=1, max_length=64)
    cargo_name: str = Field(min_length=1, max_length=200)
    hs_code: str = ""
    qty: str = ""
    total_value: float = 0
    currency: str = "CNY"
    remark: str = ""
    client_ref: Optional[str] = Field(default=None, max_length=80)
    # 离线场景：报关员在口岸无网时本地起草，恢复网络后补传
    offline_drafted: bool = False
    client_updated_at: Optional[datetime] = None


class AssignBrokerIn(BaseModel):
    broker_id: int


class TransitionIn(BaseModel):
    action: str  # enter/submit_review/inspect/release/recheck/close/cancel
    note: str = ""
    expected_version: Optional[int] = None  # 乐观锁：离线恢复合并时用
    # 离线期间在客户端发生的流转，恢复后补传
    offline_events: Optional[list[dict]] = None


class TaxPayIn(BaseModel):
    paid: bool = True
    note: str = ""


class RevealNameIn(BaseModel):
    enterprise_id: int
    # 长度在端点内校验，保证返回统一的友好错误结构（而非框架 422）
    reason: str = Field(max_length=300, description="二次确认必须填写查看理由，至少10字")


class InspectionScheduleIn(BaseModel):
    """安排查验排期（海关审单员逻辑审结通过后安排查验）。"""
    declaration_id: int
    yard_id: int
    bay_id: int
    inspector_id: int
    scheduled_at: datetime
    duration_minutes: int = Field(default=120, ge=30, le=480)
    reason: str = ""
    confirm: bool = False  # 超营业窗口/确认冲突强制落位时须二次确认


class InspectionRescheduleIn(BaseModel):
    """甘特拖拽改期：只改时间或连车位/查验员一起换。"""
    scheduled_at: datetime
    bay_id: Optional[int] = None
    inspector_id: Optional[int] = None
    reason: str = Field(default="", max_length=400)
    confirm: bool = False  # 落点超窗口或存在冲突时须二次确认填原因


class ReassignIn(BaseModel):
    """改派：车故障/人请假，从当前单解绑重派，并链式顺延后续单。"""
    reason: str = Field(min_length=4, max_length=400, description="改派原因，如：查验台位故障/查验员请假")
    new_bay_id: Optional[int] = None
    new_inspector_id: Optional[int] = None
    new_scheduled_at: Optional[datetime] = None
    confirm: bool = False  # False=只做预演(dry-run)；True=确认执行


class InspectionCancelIn(BaseModel):
    reason: str = Field(min_length=4, max_length=300)


class InspectionFinishIn(BaseModel):
    result: str = "查验无误"
    abnormal: bool = False


# ---------- 输出序列化 ----------

def user_out(u) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role.value if hasattr(u.role, "value") else u.role,
        "role_label": ROLE_LABELS[u.role],
        "enterprise_id": u.enterprise_id,
        "enterprise_name": u.enterprise.name_short if u.enterprise else None,
        "port": u.port or None,
        "qualifications": getattr(u, "qual_list", []) if u.role == Role.INSPECTOR else [],
        "qual_labels": [QUAL_LABELS.get(q, q) for q in getattr(u, "qual_list", [])],
        "on_leave": bool(getattr(u, "on_leave", False)),
    }


def enterprise_out(e, reveal_full: bool = False) -> dict:
    return {
        "id": e.id,
        "code": e.code,
        "name": e.name_full if reveal_full else e.name_short,
        "name_masked": e.name_short,
        "name_full": e.name_full if reveal_full else None,
        "full_name_revealed": reveal_full,
        "credit_code": e.credit_code if reveal_full else mask_credit(e.credit_code),
        "contact_person": e.contact_person,
        "contact_phone": e.contact_phone,
        "ie_flag": e.ie_flag,
        "status": e.status.value,
        "status_label": {
            EnterpriseStatus.FILING: "备案中",
            EnterpriseStatus.ACTIVE: "已备案",
            EnterpriseStatus.REJECTED: "备案驳回",
        }[e.status],
        "filed_at": e.filed_at.isoformat(timespec="seconds") if e.filed_at else None,
        "reject_reason": e.reject_reason or None,
    }


def mask_credit(code: str) -> str:
    if not code or len(code) < 8:
        return "****"
    return code[:4] + "****" + code[-4:]


def contract_out(c) -> dict:
    return {
        "id": c.id,
        "contract_no": c.contract_no,
        "enterprise_id": c.enterprise_id,
        "enterprise_name": c.enterprise.name_short,
        "status": c.status.value,
        "status_label": {
            ContractStatus.PENDING: "待签署",
            ContractStatus.ACTIVE: "已生效",
            ContractStatus.TERMINATED: "已终止",
        }[c.status],
        "scope_text": c.scope_text,
        "signed_at": c.signed_at.isoformat(timespec="seconds") if c.signed_at else None,
    }


def declaration_out(d, *, can_see_full_name: bool = False) -> dict:
    return {
        "id": d.id,
        "decl_no": d.decl_no,
        "enterprise_id": d.enterprise_id,
        "enterprise_name": d.enterprise.name_full if can_see_full_name else d.enterprise.name_short,
        "contract_id": d.contract_id,
        "broker_id": d.broker_id,
        "broker_name": d.broker.display_name if d.broker else None,
        "customs_officer_id": d.customs_officer_id,
        "status": d.status.value,
        "status_label": STATUS_LABELS[d.status],
        "version": d.version,
        "ie_type": d.ie_type,
        "port": d.port,
        "cargo_name": d.cargo_name,
        "hs_code": d.hs_code,
        "qty": d.qty,
        "total_value": float(d.total_value or 0),
        "currency": d.currency,
        "tax_amount": float(d.tax_amount or 0),
        "tax_due_date": d.tax_due_date.isoformat(timespec="seconds") if d.tax_due_date else None,
        "tax_paid": d.tax_paid,
        "tax_paid_at": d.tax_paid_at.isoformat(timespec="seconds") if d.tax_paid_at else None,
        "offline_created": d.offline_created,
        "remark": d.remark,
        "entrust_time": d.entrust_time.isoformat(timespec="seconds"),
        "updated_at": d.updated_at.isoformat(timespec="seconds"),
        "inspection": active_inspection_out(d),
    }


def active_inspection_out(d) -> dict | None:
    """当前生效查验排期：优先未取消的最新一条；全部取消则返回 None（前端据此渲染「排期全取消」空态）。"""
    active = [i for i in d.inspections if i.status != "cancelled"]
    if active:
        return inspection_out(active[-1])
    return None


def inspection_out(i) -> dict:
    from .models import BAY_KIND_LABELS, QUAL_LABELS
    return {
        "id": i.id,
        "declaration_id": i.declaration_id,
        "decl_no": i.declaration.decl_no,
        "enterprise_id": i.declaration.enterprise_id,
        "enterprise_name": i.declaration.enterprise.name_short,
        "decl_status": i.declaration.status.value,
        "decl_status_label": STATUS_LABELS[i.declaration.status],
        "scheduled_at": i.scheduled_at.isoformat(timespec="minutes"),
        "scheduled_end": i.end_time.isoformat(timespec="minutes"),
        "duration_minutes": i.duration_minutes or 120,
        "due_date": i.due_date.isoformat(timespec="minutes") if i.due_date else None,
        "started_at": i.started_at.isoformat(timespec="minutes") if i.started_at else None,
        "finished_at": i.finished_at.isoformat(timespec="minutes") if i.finished_at else None,
        "port": i.yard.port if i.yard else (getattr(i, "port", "") or ""),
        "yard_id": i.yard_id,
        "yard_name": i.yard.name if i.yard else None,
        "bay_id": i.bay_id,
        "bay_code": i.bay.code if i.bay else "",
        "bay_kind": i.bay.kind if i.bay else "general",
        "bay_kind_label": BAY_KIND_LABELS.get(i.bay.kind, "普货台位") if i.bay else "普货台位",
        "inspector_id": i.inspector_id,
        "inspector_name": i.inspector.display_name if i.inspector else None,
        "inspector_on_leave": bool(i.inspector.on_leave) if i.inspector else False,
        "required_qual": i.required_qual,
        "required_qual_label": QUAL_LABELS.get(i.required_qual, "") if i.required_qual else "",
        "status": i.status,
        "status_label": {
            "pending": "待查验", "inspecting": "查验中", "done": "已完成",
            "abnormal": "异常", "cancelled": "已取消",
        }.get(i.status, i.status),
        "result_note": i.result_note,
        "cancel_reason": i.cancel_reason or None,
        "cargo_name": i.declaration.cargo_name,
        "hs_code": i.declaration.hs_code,
        "reassign_count": i.reassign_count or 0,
        "chain_batch": i.chain_batch,
    }


def event_out(ev) -> dict:
    return {
        "id": ev.id,
        "actor_name": ev.actor_name,
        "from_status": ev.from_status.value if ev.from_status else None,
        "from_label": STATUS_LABELS[ev.from_status] if ev.from_status else "（立项）",
        "to_status": ev.to_status.value,
        "to_label": STATUS_LABELS[ev.to_status],
        "note": ev.note,
        "is_offline": ev.is_offline,
        "created_at": ev.created_at.isoformat(timespec="seconds"),
    }
