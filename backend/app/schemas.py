"""Pydantic 出入参模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field

from .models import (
    Role, DeclStatus, EnterpriseStatus, ContractStatus,
    ROLE_LABELS, STATUS_LABELS,
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


class InspectionCreateIn(BaseModel):
    declaration_id: int
    yard_id: int
    bay_id: int
    inspector_id: int
    scheduled_at: datetime
    scheduled_end: datetime | None = None  # 缺省按默认时长补齐
    duration_minutes: int = 120
    due_at: datetime | None = None         # 应查验日（缺省=计划开始时间）
    # 落点超出场站作业时段/排期窗口时，必须二次确认并填原因（留痕 window_override）
    window_confirmed: bool = False
    window_reason: str = ""
    remark: str = ""


class InspectionMoveIn(BaseModel):
    """拖拽改期/直接改派（不触发链式：只动当前单）。"""
    scheduled_at: datetime
    scheduled_end: datetime | None = None
    bay_id: int | None = None
    inspector_id: int | None = None
    expected_version: int | None = None
    window_confirmed: bool = False
    window_reason: str = ""


class ReassignPreviewIn(BaseModel):
    """车故障/人请假：从当前单解绑重排的预览入参。"""
    new_scheduled_at: datetime
    new_bay_id: int | None = None        # 缺省=同场站自动找合规车位
    new_inspector_id: int | None = None  # 缺省=自动找合规查验员
    duration_minutes: int = 120
    reason_type: Literal["bay_broken", "inspector_leave", "manual"] = "manual"
    reason: str = ""
    # 故障车位 / 请假查验员若未显式给新资源，自动加入禁用集合
    disable_bay_id: int | None = None
    disable_inspector_id: int | None = None


class ReassignConfirmIn(BaseModel):
    """改派确认：提交预览返回的批次方案；服务端重新推演一遍，不信客户端回传的方案。"""
    new_scheduled_at: datetime
    new_bay_id: int | None = None
    new_inspector_id: int | None = None
    duration_minutes: int = 120
    reason_type: Literal["bay_broken", "inspector_leave", "manual"] = "manual"
    reason: str = Field(min_length=5, max_length=400, description="改派原因，必填≥5字并留痕")
    disable_bay_id: int | None = None
    disable_inspector_id: int | None = None
    mark_resource_unavailable: bool = False  # 是否同步把故障车位/请假查验员置停用


class InspectionCancelIn(BaseModel):
    reason: str = Field(min_length=5, max_length=400, description="取消原因必填并留痕")


class InspectionFinishIn(BaseModel):
    result: str = "查验无误"
    abnormal: bool = False


class ReviewDecisionIn(BaseModel):
    declaration_id: int
    result: Literal["pass_inspect", "release", "return"] = "pass_inspect"
    document_ok: bool = True
    logic_ok: bool = True
    risk_tags: str = ""
    opinion: str = ""


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
        "inspection": inspection_out(d.inspections[-1]) if d.inspections else None,
    }


def inspection_out(i) -> dict:
    decl_status = i.declaration.status
    return {
        "id": i.id,
        "declaration_id": i.declaration_id,
        "decl_no": i.declaration.decl_no,
        "decl_status": decl_status.value,
        "decl_status_label": STATUS_LABELS[decl_status],
        "enterprise_id": i.declaration.enterprise_id,
        "enterprise_name": i.declaration.enterprise.name_short,
        "scheduled_at": i.scheduled_at.isoformat(timespec="minutes"),
        "scheduled_end": (i.scheduled_end or i.scheduled_at).isoformat(timespec="minutes"),
        "due_at": i.due_at.isoformat(timespec="minutes") if i.due_at else None,
        "finished_at": i.finished_at.isoformat(timespec="minutes") if i.finished_at else None,
        "yard_id": i.yard_id,
        "yard_name": i.yard.name if i.yard else None,
        "bay_id": i.bay_id,
        "port": i.port,
        "bay": (i.bay_ref.name if i.bay_ref else None) or i.bay,
        "bay_cert_tags": [t for t in (i.bay_ref.cert_tags.split(",") if i.bay_ref else []) if t],
        "inspector_id": i.inspector_id,
        "inspector_name": i.inspector.display_name if i.inspector else None,
        "inspector_certs": [t.strip() for t in ((i.inspector.inspector_certs or "").split(",")) if t.strip()]
                           if i.inspector else [],
        "status": i.status,
        "status_label": {
            "scheduled": "待查验", "inspecting": "查验中", "done": "已完成",
            "abnormal": "异常", "cancelled": "已取消",
        }.get(i.status, i.status),
        "result_note": i.result_note,
        "cargo_name": i.declaration.cargo_name,
        "hs_code": i.declaration.hs_code,
        "required_certs": [t.strip() for t in (i.required_certs or "normal").split(",") if t.strip()],
        "version": i.version,
        "reassign_batch_id": i.reassign_batch_id,
        # 终态保护标记：前端据此禁用拖拽/改派
        "locked": decl_status in (DeclStatus.RELEASED, DeclStatus.CLOSED),
    }


def yard_out(y, *, with_bays: bool = True, with_inspectors: bool = True) -> dict:
    data = {
        "id": y.id,
        "code": y.code,
        "name": y.name,
        "port": y.port,
        "address": y.address,
        "open_hour": y.open_hour,
        "close_hour": y.close_hour,
        "horizon_days": y.horizon_days,
    }
    if with_bays:
        data["bays"] = [bay_out(b) for b in y.bays]
    if with_inspectors:
        data["inspectors"] = [
            {
                "id": u.id,
                "display_name": u.display_name,
                "certs": [t.strip() for t in (u.inspector_certs or "").split(",") if t.strip()],
                "available": u.available,
                "unavailable_reason": u.unavailable_reason or None,
            }
            for u in y.inspectors
        ]
    return data


def bay_out(b) -> dict:
    return {
        "id": b.id,
        "yard_id": b.yard_id,
        "code": b.code,
        "name": b.name or b.code,
        "cert_tags": [t.strip() for t in (b.cert_tags or "normal").split(",") if t.strip()],
        "seq": b.seq,
        "out_of_service": b.out_of_service,
        "out_of_service_reason": b.out_of_service_reason or None,
    }


def schedule_log_out(lg) -> dict:
    import json
    detail = None
    if lg.detail:
        try:
            detail = json.loads(lg.detail)
        except Exception:
            detail = lg.detail
    return {
        "id": lg.id,
        "inspection_id": lg.inspection_id,
        "declaration_id": lg.declaration_id,
        "actor_name": lg.actor_name,
        "action": lg.action,
        "action_label": {
            "create": "创建排期", "move": "拖拽改期", "reassign": "解绑改派",
            "chain_move": "链式顺延", "cancel": "取消排期", "finish": "登记结果",
            "window_override": "超窗口确认",
        }.get(lg.action, lg.action),
        "reassign_batch_id": lg.reassign_batch_id,
        "reason": lg.reason,
        "detail": detail,
        "created_at": lg.created_at.isoformat(timespec="seconds"),
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
