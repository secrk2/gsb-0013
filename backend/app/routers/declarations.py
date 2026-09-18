"""报关单：立项派单、状态流转（状态机强校验）、征税、查验、离线恢复合并。

关键设计：
- 立项必须经过「企业已备案 + 合同已生效」链路校验；
- Idempotency-Key / client_ref 双保险，断网恢复重放绝不产生重复报关单；
- 流转走 models.guard_transition：非法回退、越权角色一律拦下并返回可读原因；
- expected_version 乐观锁：客户端带的版本过期 → 409 version_conflict 并回传服务端快照，
  前端据此提示「合并」而非覆盖；
- offline_events：报关员口岸断网期间在本地记录的动作，联网后顺序补传，
  每条带客户端事件 ID 去重，已应用的跳过，冲突的进冲突清单，不静默丢数据。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
import json

from ..database import get_db
from ..models import (
    User, Role, Enterprise, Contract, Declaration, DeclarationEvent, Inspection,
    IdempotencyRecord,
    DeclStatus, BizError, guard_transition,
)
from ..schemas import (
    DeclarationCreateIn, AssignBrokerIn, TransitionIn, TaxPayIn,
    declaration_out, event_out, inspection_out,
)
from ..deps import get_current_user, require_roles, load_declaration_scoped, ensure_active_contract

router = APIRouter(tags=["declarations"])

# 前端动作按钮 → 状态机 (from, to)
ACTION_MAP: dict[str, tuple[DeclStatus, DeclStatus]] = {
    "enter": (DeclStatus.ENTRUSTED, DeclStatus.ENTERED),
    "submit_review": (DeclStatus.ENTERED, DeclStatus.REVIEWING),
    "reject_to_enter": (DeclStatus.REVIEWING, DeclStatus.ENTERED),
    "inspect": (DeclStatus.REVIEWING, DeclStatus.INSPECTING),
    "release_from_review": (DeclStatus.REVIEWING, DeclStatus.RELEASED),
    "release_from_inspection": (DeclStatus.INSPECTING, DeclStatus.RELEASED),
    "recheck": (DeclStatus.INSPECTING, DeclStatus.REVIEWING),
    "close": (DeclStatus.RELEASED, DeclStatus.CLOSED),
    "cancel_entrusted": (DeclStatus.ENTRUSTED, DeclStatus.CANCELLED),
    "cancel_entered": (DeclStatus.ENTERED, DeclStatus.CANCELLED),
    "cancel_reviewing": (DeclStatus.REVIEWING, DeclStatus.CANCELLED),
}

ACTION_LABELS = {
    "enter": "录入报关单",
    "submit_review": "提交海关审单",
    "reject_to_enter": "审单退回补录",
    "inspect": "布控查验",
    "release_from_review": "审结放行",
    "release_from_inspection": "查验后放行",
    "recheck": "查验异常转重审",
    "close": "办结结关",
    "cancel_entrusted": "撤销委托",
    "cancel_entered": "撤单",
    "cancel_reviewing": "审单撤销",
}


def _gen_decl_no(db: Session, ie_type: str) -> str:
    prefix = "I" if ie_type == "import" else "E"
    today = datetime.utcnow().strftime("%Y%m%d")
    count = db.query(Declaration).count() + 1
    return f"{prefix}{today}{count:05d}"


def _add_event(db, decl, user, from_s, to_s, note, is_offline=False, idem_key=None):
    ev = DeclarationEvent(
        declaration_id=decl.id,
        actor_id=user.id,
        actor_name=user.display_name,
        from_status=from_s,
        to_status=to_s,
        note=note or "",
        is_offline=is_offline,
        idempotency_key=idem_key,
    )
    db.add(ev)


@router.get("/declarations")
def list_declarations(status: str | None = None,
                      enterprise_id: int | None = None,
                      port: str | None = None,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    q = db.query(Declaration)
    # 企业间隔离：企业管理员只能列出本企业单据（越权条件直接 403，见详情接口）
    if user.role == Role.ENTERPRISE:
        if enterprise_id and enterprise_id != user.enterprise_id:
            raise BizError("越权拦截：不能查询其他企业的报关单列表",
                           code="cross_enterprise_forbidden", status_code=403)
        q = q.filter(Declaration.enterprise_id == user.enterprise_id)
    elif enterprise_id:
        q = q.filter(Declaration.enterprise_id == enterprise_id)
    if status:
        try:
            q = q.filter(Declaration.status == DeclStatus(status))
        except ValueError:
            raise BizError(f"未知状态筛选：{status}", code="bad_status", status_code=400)
    if port:
        q = q.filter(Declaration.port == port)
    rows = q.order_by(Declaration.id.desc()).all()
    return [declaration_out(d) for d in rows]


@router.post("/declarations")
def create_declaration(body: DeclarationCreateIn,
                       user: User = Depends(get_current_user),
                       idempotency_key: str | None = Header(default=None),
                       db: Session = Depends(get_db)):
    # —— 第一道幂等：Idempotency-Key（断网恢复后前端自动重试同一把钥匙）——
    if idempotency_key:
        idem_key = idempotency_key.strip()[:80]
        rec = db.query(IdempotencyRecord).filter(
            IdempotencyRecord.user_id == user.id,
            IdempotencyRecord.idempotency_key == idem_key,
        ).first()
        if rec:
            payload = json.loads(rec.response_body)
            payload.setdefault("deduplicated", True)
            payload["idempotency"] = {
                "replayed": True,
                "key": idem_key,
                "first_response_at": rec.created_at.isoformat(timespec="seconds"),
                "notice": "检测到重复提交（同一 Idempotency-Key），已返回首次结果，未产生重复报关单。",
            }
            return payload
    else:
        idem_key = None

    # 职责：本行报关员立项；企业管理员可对本企业发起委托
    if user.role == Role.ENTERPRISE:
        if body.enterprise_id != user.enterprise_id:
            raise BizError("越权拦截：不能为其他企业发起报关委托",
                           code="cross_enterprise_forbidden", status_code=403)
    elif user.role not in (Role.BROKER, Role.SUPERVISOR):
        raise BizError("当前角色无权立项报关单（需本行报关员）",
                       code="role_forbidden", status_code=403)

    # —— 委托到客户链路：备案 → 合同 → 立项，缺一拦下来说原因 ——
    ensure_active_contract(body.enterprise_id, body.contract_id, db)

    # —— 第二道幂等：同一企业同一委托引用只允许一张单（客户端草稿重试压舱）——
    if body.client_ref:
        dup = db.query(Declaration).filter(
            Declaration.enterprise_id == body.enterprise_id,
            Declaration.client_ref == body.client_ref,
        ).first()
        if dup:
            resp = {
                "ok": True,
                "deduplicated": True,
                "declaration": declaration_out(dup),
                "hint": f"该委托此前已立项（{dup.decl_no}），本次为重试请求，已幂等返回，未产生重复报关单。",
            }
            _store_idem(db, user, idem_key, "/declarations", resp)
            return resp

    decl = Declaration(
        decl_no=_gen_decl_no(db, body.ie_type),
        client_ref=body.client_ref,
        enterprise_id=body.enterprise_id,
        contract_id=body.contract_id,
        broker_id=user.id if user.role == Role.BROKER else None,
        status=DeclStatus.ENTRUSTED,
        version=1,
        ie_type=body.ie_type,
        port=body.port,
        cargo_name=body.cargo_name,
        hs_code=body.hs_code,
        qty=body.qty,
        total_value=body.total_value,
        currency=body.currency,
        remark=body.remark,
        offline_created=body.offline_drafted,
    )
    db.add(decl)
    db.flush()
    _add_event(db, decl, user, None, DeclStatus.ENTRUSTED,
               "企业委托立项" + ("（口岸离线起草，联网补传）" if body.offline_drafted else ""),
               is_offline=body.offline_drafted)
    db.commit()
    db.refresh(decl)
    resp = {"ok": True, "deduplicated": False, "declaration": declaration_out(decl)}
    _store_idem(db, user, idem_key, "/declarations", resp)
    return resp


def _store_idem(db: Session, user: User, key: str | None, path: str, resp: dict) -> None:
    if not key:
        return
    db.add(IdempotencyRecord(
        user_id=user.id,
        idempotency_key=key,
        method="POST",
        path=path,
        response_body=json.dumps(resp, ensure_ascii=False),
        response_code=200,
    ))
    db.commit()


@router.post("/declarations/{decl_id}/assign")
def assign_broker(decl_id: int, body: AssignBrokerIn,
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    decl = load_declaration_scoped(decl_id, user, db)
    if user.role not in (Role.BROKER, Role.SUPERVISOR):
        raise BizError("仅本行可派单给报关员", code="role_forbidden", status_code=403)
    broker = db.get(User, body.broker_id)
    if not broker or broker.role != Role.BROKER:
        raise BizError("指派对象不是有效报关员账号", code="bad_broker", status_code=400)
    if decl.status != DeclStatus.ENTRUSTED:
        raise BizError(f"报关单已进入「{decl.status.value}」环节，不能再改派报关员",
                       code="assign_too_late")
    decl.broker_id = broker.id
    decl.version += 1
    _add_event(db, decl, user, DeclStatus.ENTRUSTED, DeclStatus.ENTRUSTED,
               f"派单给报关员 {broker.display_name}")
    db.commit()
    return {"ok": True, "declaration": declaration_out(decl)}


@router.get("/declarations/{decl_id}")
def get_declaration(decl_id: int,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    decl = load_declaration_scoped(decl_id, user, db)
    can_full = False  # 列表/详情默认始终脱敏；看全名必须走 /reveal 二次确认留痕
    return {
        "declaration": declaration_out(decl, can_see_full_name=can_full),
        "enterprise": {
            "id": decl.enterprise.id,
            "name_masked": decl.enterprise.name_short,
            "status": decl.enterprise.status.value,
        },
        "contract_no": decl.enterprise and db.get(Contract, decl.contract_id).contract_no,
        "events": [event_out(e) for e in decl.events],
        # 全部查验记录（含已取消）：前端据此区分「该单无排期」与「排期全部取消」两种空态
        "inspections": [inspection_out(i) for i in decl.inspections],
        "allowed_actions": allowed_actions_for(decl, user),
    }


def allowed_actions_for(decl: Declaration, user: User) -> list[dict]:
    """按当前状态+角色给前端可渲染的操作按钮。"""
    from ..models import ALLOWED_TRANSITIONS, TRANSITION_ROLES, ROLE_LABELS
    result = []
    for action, (frm, to) in ACTION_MAP.items():
        if frm != decl.status:
            continue
        if to not in ALLOWED_TRANSITIONS.get(frm, {}):
            continue
        roles = TRANSITION_ROLES.get((frm, to), set())
        if user.role in roles:
            result.append({"action": action, "label": ACTION_LABELS[action],
                           "to_status": to.value})
    return result


@router.post("/declarations/{decl_id}/transition")
def transition(decl_id: int, body: TransitionIn,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    decl = load_declaration_scoped(decl_id, user, db)

    if body.action not in ACTION_MAP:
        raise BizError(f"未知业务动作：{body.action}", code="bad_action", status_code=400)
    frm, to = ACTION_MAP[body.action]

    # 乐观锁：前端基于旧版本操作时先拦下，回传服务端现状，由前端合并后再决定
    if body.expected_version is not None and body.expected_version != decl.version:
        raise BizError(
            f"版本冲突：您看到的是 v{body.expected_version}，服务端已到 v{decl.version}"
            f"（当前状态：{decl.status.value}）。页面将合并最新状态后再提交，不会覆盖他人变更。",
            code="version_conflict",
            status_code=409,
        )

    # guard_transition 内部完成「非法回退 / 终态 / 角色越权」三类拦截并给原因
    guard_transition(decl.status, to, user.role)

    _apply_transition(db, decl, user, frm, to, body.note)
    db.commit()
    db.refresh(decl)
    return {"ok": True, "declaration": declaration_out(decl),
            "allowed_actions": allowed_actions_for(decl, user)}


def _apply_transition(db, decl, user, frm, to, note, is_offline=False, idem_key=None):
    if to == DeclStatus.CLOSED:
        # 结关前尚有未完成查验的不允许——状态机已限定从已放行来，这里补一道业务检查
        open_insp = [i for i in decl.inspections if i.status in ("pending", "inspecting", "abnormal")]
        if open_insp:
            raise BizError("尚有未完成的查验排期，不能结关，请先在作战台处置查验任务",
                           code="inspection_open")
    decl.status = to
    decl.version += 1
    if to == DeclStatus.REVIEWING and decl.customs_officer_id is None and user.role == Role.CUSTOMS:
        decl.customs_officer_id = user.id
    _add_event(db, decl, user, frm, to, note, is_offline=is_offline, idem_key=idem_key)


@router.post("/declarations/{decl_id}/sync-offline")
def sync_offline(decl_id: int, body: TransitionIn,
                 user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """口岸断网恢复后的离线变更合并。

    入参 offline_events: [{client_event_id, action, note, client_at}]
    - 已按 client_event_id 同步过的事件 → 跳过（幂等）；
    - 顺序在当前服务端状态上重放，逐条过状态机；
    - 某条非法（例如服务端已被海关推进，本地还想回退）→ 进 conflicts，不强行覆盖；
    - 全程在同一事务，任何一条都不会造成重复报关单。
    """
    decl = load_declaration_scoped(decl_id, user, db)
    if user.role != Role.BROKER:
        raise BizError("离线作业合并仅面向口岸报关员账号", code="role_forbidden", status_code=403)
    if not body.offline_events:
        raise BizError("没有待合并的离线事件", code="empty_offline_batch", status_code=400)

    applied, skipped, conflicts = [], [], []
    existing_keys = {
        e.idempotency_key for e in db.query(DeclarationEvent)
        .filter(DeclarationEvent.declaration_id == decl.id,
                DeclarationEvent.idempotency_key.isnot(None)).all()
    }

    for ev in body.offline_events:
        cev_id = str(ev.get("client_event_id") or "").strip()
        action = ev.get("action")
        note = ev.get("note", "")
        if not cev_id:
            conflicts.append({"event": ev, "reason": "离线事件缺少 client_event_id，无法去重，已拒绝合入"})
            continue
        if cev_id in existing_keys:
            skipped.append({"client_event_id": cev_id, "reason": "此前已同步，幂等跳过"})
            continue
        if action not in ACTION_MAP:
            conflicts.append({"client_event_id": cev_id, "reason": f"未知动作 {action}"})
            continue

        frm, to = ACTION_MAP[action]
        try:
            guard_transition(decl.status, to, user.role)
            _apply_transition(db, decl, user, decl.status, to,
                              f"[离线补传] {note}", is_offline=True, idem_key=cev_id)
            existing_keys.add(cev_id)
            applied.append({"client_event_id": cev_id, "action": action,
                            "to_status": to.value})
        except BizError as exc:
            # 与服务端现状冲突（如本地旧状态想往回走）→ 记录冲突，交人工在页面确认，不糊弄
            conflicts.append({
                "client_event_id": cev_id,
                "action": action,
                "reason": exc.reason,
                "server_status": decl.status.value,
                "server_version": decl.version,
            })

    db.commit()
    db.refresh(decl)
    return {
        "ok": True,
        "merged": {
            "applied": applied,
            "skipped_duplicates": skipped,
            "conflicts": conflicts,
        },
        "declaration": declaration_out(decl),
        "server_snapshot": {
            "status": decl.status.value,
            "version": decl.version,
            "updated_at": decl.updated_at.isoformat(timespec="seconds"),
        },
    }


# ---------- 征税 ----------

@router.post("/declarations/{decl_id}/tax")
def pay_tax(decl_id: int, body: TaxPayIn,
            user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    decl = load_declaration_scoped(decl_id, user, db)
    if user.role not in (Role.BROKER, Role.ENTERPRISE, Role.CUSTOMS):
        raise BizError("当前角色无权登记缴税", code="role_forbidden", status_code=403)
    if float(decl.tax_amount or 0) <= 0:
        raise BizError("该单尚未生成应征税款，不能登记缴税", code="no_tax")
    if decl.tax_paid:
        raise BizError("税款已缴纳，重复缴税登记被幂等拦截", code="tax_already_paid")
    decl.tax_paid = True
    decl.tax_paid_at = datetime.utcnow()
    decl.version += 1
    _add_event(db, decl, user, decl.status, decl.status, f"登记缴税 ¥{float(decl.tax_amount):,.2f} {body.note}")
    db.commit()
    return {"ok": True, "declaration": declaration_out(decl)}


# ---------- 查验排期（写操作在 /api/scheduling 路由；此处保留只读列表与便捷动作） ----------

@router.get("/inspections")
def list_inspections(status: str | None = None,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Inspection)
    if user.role == Role.ENTERPRISE:
        q = q.join(Declaration).filter(Declaration.enterprise_id == user.enterprise_id)
    if status:
        q = q.filter(Inspection.status == status)
    return [inspection_out(i) for i in q.order_by(Inspection.scheduled_at).all()]
