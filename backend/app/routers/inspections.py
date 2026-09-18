"""审单与查验：逻辑审核 → 排期 → 甘特拖拽 → 改派链式重排 → 登记结果。

设计要点：
- 必须有海关审单员「逻辑审核通过(pass_inspect)」结论，才能排查验计划；
- 所有落位先过冲突引擎：硬冲突 409 + 逐条结构化明细（前端标红、点开看原因），
  超作业窗口为软拦截，必须 window_confirmed + 填原因，写 window_override 留痕；
- 改派（车故障/人请假）走「预览 → 确认」两步，确认时服务端重新推演，不信客户端方案；
  链式顺延只在同场站、只向后推，已放行/已结关单绝不移动；
- 及时率双口径同时给出，口径公式直接随接口返回，界面原样展示。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    User, Role, Yard, Bay, Declaration, Inspection, ReviewDecision, ScheduleLog,
    DeclStatus, BizError, guard_transition,
)
from ..schemas import (
    ReviewDecisionIn, InspectionCreateIn, InspectionMoveIn,
    ReassignPreviewIn, ReassignConfirmIn, InspectionCancelIn, InspectionFinishIn,
    inspection_out, yard_out, schedule_log_out,
)
from ..deps import get_current_user, load_declaration_scoped
from ..services import scheduling as sched

router = APIRouter(tags=["inspections"])

PLAN_ROLES = (Role.CUSTOMS, Role.SUPERVISOR)


# ---------- 工具 ----------

def _load_yard(db: Session, yard_id: int) -> Yard:
    yard = db.get(Yard, yard_id)
    if not yard:
        raise BizError(f"监管场站 #{yard_id} 不存在", code="yard_not_found", status_code=404)
    return yard


def _load_inspection(db: Session, insp_id: int, user: User) -> Inspection:
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError(f"查验排期 #{insp_id} 不存在", code="not_found", status_code=404)
    # 企业间隔离
    if user.role == Role.ENTERPRISE and user.enterprise_id != insp.declaration.enterprise_id:
        raise BizError("越权访问拦截：该查验排期属于其他企业，您无权查看。",
                       code="cross_enterprise_forbidden", status_code=403)
    return insp


def _require_plan_role(user: User) -> None:
    if user.role not in PLAN_ROLES:
        raise BizError("安排/调整查验排期需海关审单员或监管员权限",
                       code="role_forbidden", status_code=403)


def _resolve_targets(db: Session, yard: Yard, bay_id: int | None, inspector_id: int | None):
    bay = None
    if bay_id is not None:
        bay = db.get(Bay, bay_id)
        if not bay or bay.yard_id != yard.id:
            raise BizError(f"车位 #{bay_id} 不属于场站「{yard.name}」，不能跨场站占车位",
                           code="bay_not_in_yard", status_code=400)
    inspector = None
    if inspector_id is not None:
        inspector = db.get(User, inspector_id)
        if not inspector or inspector.role != Role.INSPECTOR or inspector.yard_id != yard.id:
            raise BizError(f"查验员 #{inspector_id} 不属于场站「{yard.name}」或不是查验员账号",
                           code="inspector_not_in_yard", status_code=400)
    return bay, inspector


def _add_log(db, insp, user, action, reason="", detail=None, batch_id=None):
    db.add(ScheduleLog(
        inspection_id=insp.id,
        declaration_id=insp.declaration_id,
        actor_id=user.id if user else None,
        actor_name=user.display_name if user else "系统",
        action=action,
        reason=reason or "",
        detail=json.dumps(detail, ensure_ascii=False) if detail is not None else "",
        reassign_batch_id=batch_id,
    ))


def _end_time(start: datetime, end: datetime | None, duration_minutes: int) -> datetime:
    if end is not None:
        if end <= start:
            raise BizError("计划结束时间必须晚于开始时间", code="bad_time_range", status_code=400)
        return end
    return start + timedelta(minutes=max(15, duration_minutes))


def _scoped_inspection_query(db: Session, user: User):
    q = db.query(Inspection)
    if user.role == Role.ENTERPRISE:
        q = q.join(Declaration).filter(Declaration.enterprise_id == user.enterprise_id)
    return q


# ---------- 逻辑审核 ----------

@router.post("/review-decisions")
def create_review_decision(body: ReviewDecisionIn,
                           user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """海关审单员逻辑审核：单证一致 + 归类/价格逻辑。

    pass_inspect 审核通过布控 → 保持审单中，等待排查验计划；
    release 审结无查验放行；return 退回补录。
    """
    if user.role not in (Role.CUSTOMS, Role.SUPERVISOR):
        raise BizError("逻辑审核仅海关审单员可执行", code="role_forbidden", status_code=403)
    decl = load_declaration_scoped(body.declaration_id, user, db)
    if decl.status != DeclStatus.REVIEWING:
        raise BizError(
            f"报关单当前为「{decl.status.value}」，只有「审单中」的单据能做逻辑审核结论。",
            code="bad_status_for_review", status_code=409)

    decision = ReviewDecision(
        declaration_id=decl.id,
        officer_id=user.id,
        officer_name=user.display_name,
        result=body.result,
        document_ok=body.document_ok,
        logic_ok=body.logic_ok,
        risk_tags=(body.risk_tags or "").strip()[:200],
        opinion=(body.opinion or "").strip()[:400],
    )

    if body.result == "pass_inspect":
        if not (body.document_ok and body.logic_ok):
            raise BizError("审核结论为「通过布控」时，单证一致性与逻辑审核必须均为通过；"
                           "如有不符请选择「退回补录」。", code="review_contradiction", status_code=400)
        db.add(decision)
        db.flush()
        db.commit()
        db.refresh(decision)
        return {"ok": True, "result": "pass_inspect",
                "hint": "逻辑审核已通过，请在监管场站安排查验排期（车位与查验员）。",
                "decision_id": decision.id}

    # 审结放行 / 退回补录：直接走状态机（角色/非法回退原因由 guard 统一给）
    target = {
        "release": (DeclStatus.RELEASED, "release_from_review", "逻辑审核审结，无查验放行"),
        "return": (DeclStatus.ENTERED, "reject_to_enter", "逻辑审核退回补录"),
    }[body.result]
    to_status, _action, note = target
    guard_transition(decl.status, to_status, user.role)
    db.add(decision)
    decl.status = to_status
    decl.version += 1
    if decl.customs_officer_id is None and user.role == Role.CUSTOMS:
        decl.customs_officer_id = user.id
    from .declarations import _add_event
    _add_event(db, decl, user, DeclStatus.REVIEWING, to_status,
               f"{note}。审核意见：{body.opinion or '无'}")
    db.commit()
    return {"ok": True, "result": body.result, "declaration_status": to_status.value}


@router.get("/declarations/{decl_id}/review-decisions")
def list_review_decisions(decl_id: int,
                          user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    decl = load_declaration_scoped(decl_id, user, db)
    return [{
        "id": d.id,
        "result": d.result,
        "result_label": {"pass_inspect": "通过·布控查验", "release": "审结放行", "return": "退回补录"}[d.result],
        "document_ok": d.document_ok,
        "logic_ok": d.logic_ok,
        "risk_tags": d.risk_tags,
        "opinion": d.opinion,
        "officer_name": d.officer_name,
        "created_at": d.created_at.isoformat(timespec="seconds"),
    } for d in decl.review_decisions]


# ---------- 场站资源 ----------

@router.get("/yards")
def list_yards(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    yards = db.query(Yard).order_by(Yard.id).all()
    return [yard_out(y) for y in yards]


@router.post("/inspectors/{inspector_id}/availability")
def set_inspector_availability(inspector_id: int, payload: dict,
                               user: User = Depends(get_current_user),
                               db: Session = Depends(get_db)):
    """登记查验员请假/销假。请假后其名下未开始排期可走改派链。"""
    _require_plan_role(user)
    insp = db.get(User, inspector_id)
    if not insp or insp.role != Role.INSPECTOR:
        raise BizError("目标账号不是查验员", code="not_found", status_code=404)
    available = bool(payload.get("available", True))
    reason = (payload.get("reason") or "").strip()[:200]
    if not available and len(reason) < 2:
        raise BizError("登记请假/停用必须填写原因", code="reason_required", status_code=400)
    insp.available = available
    insp.unavailable_reason = "" if available else reason
    db.commit()
    return {"ok": True, "inspector_id": insp.id, "available": insp.available,
            "unavailable_reason": insp.unavailable_reason or None}


@router.post("/bays/{bay_id}/service")
def set_bay_service(bay_id: int, payload: dict,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """登记车位故障/恢复。故障车位上的未开始排期可走车故障改派链。"""
    _require_plan_role(user)
    bay = db.get(Bay, bay_id)
    if not bay:
        raise BizError("车位不存在", code="not_found", status_code=404)
    in_service = bool(payload.get("in_service", True))
    reason = (payload.get("reason") or "").strip()[:200]
    if not in_service and len(reason) < 2:
        raise BizError("登记车位故障必须填写原因", code="reason_required", status_code=400)
    bay.out_of_service = not in_service
    bay.out_of_service_reason = "" if in_service else reason
    db.commit()
    return {"ok": True, "bay_id": bay.id, "out_of_service": bay.out_of_service,
            "out_of_service_reason": bay.out_of_service_reason or None}


# ---------- 排期查询 / 甘特 ----------

@router.get("/inspections")
def list_inspections(yard_id: int | None = None,
                     status_filter: str | None = None,
                     date_from: datetime | None = None,
                     date_to: datetime | None = None,
                     declaration_id: int | None = None,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    q = _scoped_inspection_query(db, user)
    if yard_id:
        q = q.filter(Inspection.yard_id == yard_id)
    if status_filter:
        q = q.filter(Inspection.status == status_filter)
    if date_from:
        q = q.filter(Inspection.scheduled_end > date_from)
    if date_to:
        q = q.filter(Inspection.scheduled_at < date_to)
    if declaration_id:
        q = q.filter(Inspection.declaration_id == declaration_id)
    rows = q.order_by(Inspection.scheduled_at).all()
    now = datetime.utcnow()
    result = []
    for i in rows:
        item = inspection_out(i)
        if i.status in sched.ACTIVE_STATUSES and i.yard_id:
            item["conflicts"] = sched.find_hard_conflicts(
                db, yard_id=i.yard_id, bay=i.bay_ref, inspector=i.inspector,
                start=i.scheduled_at, end=i.scheduled_end,
                required_certs=list(sched.split_certs(i.required_certs) or {"normal"}),
                exclude_inspection_id=i.id)
            item["window_warnings"] = sched.find_window_warnings(
                i.yard, i.scheduled_at, i.scheduled_end, now)
        else:
            item["conflicts"] = []
            item["window_warnings"] = []
        result.append(item)
    return result


@router.get("/inspections/{insp_id}")
def inspection_detail(insp_id: int,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    insp = _load_inspection(db, insp_id, user)
    return {
        "inspection": inspection_out(insp),
        "logs": [schedule_log_out(lg) for lg in insp.logs],
        "declaration_status": insp.declaration.status.value,
    }


def _range_for(view: str, date: str | None) -> tuple[datetime, datetime, str]:
    if date:
        try:
            anchor = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise BizError("date 需为 YYYY-MM-DD", code="bad_date", status_code=400)
    else:
        anchor = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if view == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        return monday, monday + timedelta(days=7), monday.strftime("%Y-%m-%d")
    return anchor, anchor + timedelta(days=1), anchor.strftime("%Y-%m-%d")


@router.get("/scheduling/gantt")
def gantt(yard_id: int, view: str = "day", date: str | None = None,
          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if view not in ("day", "week"):
        raise BizError("view 只支持 day / week", code="bad_view", status_code=400)
    yard = _load_yard(db, yard_id)
    start, end, anchor = _range_for(view, date)
    # 多取前后 6 小时，边界上跨段的条也能渲染
    q = _scoped_inspection_query(db, user).filter(
        Inspection.yard_id == yard_id,
        Inspection.scheduled_end > start - timedelta(hours=6),
        Inspection.scheduled_at < end + timedelta(hours=6),
    )
    rows = q.order_by(Inspection.scheduled_at).all()
    now = datetime.utcnow()
    items = []
    for i in rows:
        item = inspection_out(i)
        # 甘特加载即逐条体检：硬冲突（撞车位/撞查验员/资质不符/资源停用）标红，点开看原因。
        # 只对占位中的排期检测；已完成/已取消单不产生新冲突。
        if i.status in sched.ACTIVE_STATUSES:
            item["conflicts"] = sched.find_hard_conflicts(
                db, yard_id=yard.id, bay=i.bay_ref, inspector=i.inspector,
                start=i.scheduled_at, end=i.scheduled_end,
                required_certs=list(sched.split_certs(i.required_certs) or {"normal"}),
                exclude_inspection_id=i.id)
            item["window_warnings"] = sched.find_window_warnings(
                yard, i.scheduled_at, i.scheduled_end, now)
        else:
            item["conflicts"] = []
            item["window_warnings"] = []
        items.append(item)
    return {
        "yard": yard_out(yard),
        "view": view,
        "anchor_date": anchor,
        "range": {"start": start.isoformat(timespec="minutes"),
                  "end": end.isoformat(timespec="minutes")},
        "now": now.isoformat(timespec="minutes"),
        "inspections": items,
        "work_window": {
            "open_hour": yard.open_hour, "close_hour": yard.close_hour,
            "horizon_days": yard.horizon_days,
        },
    }


@router.get("/scheduling/suggest")
def suggest(yard_id: int, declaration_id: int,
            duration_minutes: int = 120,
            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    yard = _load_yard(db, yard_id)
    decl = load_declaration_scoped(declaration_id, user, db)
    required = sched.infer_required_certs(decl.hs_code, decl.cargo_name)
    slots = sched.suggest_slots(
        db, yard=yard, required_certs=required, duration_minutes=duration_minutes)
    return {"required_certs": required,
            "required_cert_labels": sched.cert_labels(required),
            "slots": slots}


# ---------- 冲突预检 / 创建排期 ----------

def _check_window_or_raise(yard, start, end, confirmed, reason):
    warnings = sched.find_window_warnings(yard, start, end, datetime.utcnow())
    if warnings and not confirmed:
        raise BizError(
            "落点超出场站排期窗口，需要二次确认并填写原因后才能保存（系统将留痕）。",
            code="window_confirmation_required", status_code=409, details=warnings)
    if warnings and confirmed:
        if len(reason.strip()) < 5:
            raise BizError("超窗口排期必须填写不少于 5 个字的原因（写入留痕，供监管审计）。",
                           code="window_reason_required", status_code=400, details=warnings)
    return warnings


@router.post("/inspections/conflicts")
def dry_run_conflicts(body: InspectionCreateIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """排期表单/拖拽悬停时的实时预检：返回硬冲突 + 窗口提醒，不落库。"""
    _require_plan_role(user)
    yard = _load_yard(db, body.yard_id)
    decl = load_declaration_scoped(body.declaration_id, user, db)
    start = body.scheduled_at
    end = _end_time(start, body.scheduled_end, body.duration_minutes)
    bay, inspector = _resolve_targets(db, yard, body.bay_id, body.inspector_id)
    required = sched.infer_required_certs(decl.hs_code, decl.cargo_name)
    # 预检该单已有排期时，排除它自身（否则新落点会和自己的旧时段相撞）
    self_insp = db.query(Inspection).filter(
        Inspection.declaration_id == decl.id,
        Inspection.status.in_(sched.ACTIVE_STATUSES)).first()
    hard = sched.find_hard_conflicts(
        db, yard_id=yard.id, bay=bay, inspector=inspector,
        start=start, end=end, required_certs=required,
        exclude_inspection_id=self_insp.id if self_insp else None)
    soft = sched.find_window_warnings(yard, start, end, datetime.utcnow())
    return {"ok": not hard, "hard_conflicts": hard, "window_warnings": soft,
            "required_certs": required,
            "required_cert_labels": sched.cert_labels(required)}


@router.post("/inspections")
def create_inspection(body: InspectionCreateIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    _require_plan_role(user)
    yard = _load_yard(db, body.yard_id)
    decl = load_declaration_scoped(body.declaration_id, user, db)
    if decl.status not in (DeclStatus.REVIEWING, DeclStatus.INSPECTING):
        raise BizError(
            f"报关单当前为「{decl.status.value}」，只有审单中/查验中的单据才能排查验计划。",
            code="bad_status_for_inspection")

    # 前置：逻辑审核通过
    if decl.status == DeclStatus.REVIEWING:
        passed = any(d.result == "pass_inspect" for d in decl.review_decisions)
        if not passed:
            raise BizError(
                "该单尚未完成海关逻辑审核：请审单员先做「逻辑审核通过（布控查验）」结论，"
                "再安排查验排期。审核未通过不得直接占车位/派查验员。",
                code="review_required")

    start = body.scheduled_at
    end = _end_time(start, body.scheduled_end, body.duration_minutes)
    bay, inspector = _resolve_targets(db, yard, body.bay_id, body.inspector_id)
    required = sched.infer_required_certs(decl.hs_code, decl.cargo_name)

    hard = sched.find_hard_conflicts(
        db, yard_id=yard.id, bay=bay, inspector=inspector,
        start=start, end=end, required_certs=required)
    if hard:
        raise BizError("排期未通过冲突校验，未占用任何资源，请按下列冲突逐条调整。",
                       code="schedule_conflict", status_code=409, details=hard)
    warnings = _check_window_or_raise(yard, start, end, body.window_confirmed, body.window_reason)

    insp = Inspection(
        declaration_id=decl.id,
        yard_id=yard.id,
        bay_id=bay.id if bay else None,
        scheduled_at=start,
        scheduled_end=end,
        due_at=body.due_at or start,
        port=yard.port or decl.port,
        bay=(bay.name or bay.code) if bay else "",
        inspector_id=inspector.id if inspector else None,
        status="scheduled",
        required_certs=",".join(required),
        version=1,
    )
    db.add(insp)
    db.flush()

    if decl.status == DeclStatus.REVIEWING:
        guard_transition(decl.status, DeclStatus.INSPECTING, user.role)
        decl.status = DeclStatus.INSPECTING
        decl.version += 1
        if decl.customs_officer_id is None and user.role == Role.CUSTOMS:
            decl.customs_officer_id = user.id
        from .declarations import _add_event
        _add_event(db, decl, user, DeclStatus.REVIEWING, DeclStatus.INSPECTING,
                   f"逻辑审核通过，布控查验并排期 {yard.name} {bay.name if bay else ''} "
                   f"{start:%Y-%m-%d %H:%M}")

    _add_log(db, insp, user, "create", body.remark,
             {"at": start.isoformat(timespec="minutes"),
              "end": end.isoformat(timespec="minutes"),
              "bay": bay.name if bay else None, "inspector": inspector.display_name if inspector else None,
              "required_certs": required})
    for w in warnings:
        _add_log(db, insp, user, "window_override", body.window_reason, w)
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp),
            "window_warnings": warnings}


# ---------- 拖拽改期（单条，不链推） ----------

@router.post("/inspections/{insp_id}/move")
def move_inspection(insp_id: int, body: InspectionMoveIn,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    _require_plan_role(user)
    insp = _load_inspection(db, insp_id, user)
    if insp.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED):
        raise BizError("该报关单已放行/已结关，查验排期锁定，不能再拖拽调整。",
                       code="inspection_locked")
    if insp.status not in ("scheduled", "inspecting"):
        raise BizError(f"排期状态为「{insp.status}」，不可拖拽改期（已取消/已完成请新建排期）。",
                       code="inspection_not_movable")
    if body.expected_version is not None and body.expected_version != insp.version:
        raise BizError(
            f"排期版本冲突：您基于 v{body.expected_version} 拖拽，服务端已到 v{insp.version}，"
            "请刷新甘特获取最新排期后再拖，避免覆盖他人调整。",
            code="version_conflict", status_code=409)

    yard = insp.yard
    bay = insp.bay_ref
    inspector = insp.inspector
    if body.bay_id is not None:
        bay, _ = _resolve_targets(db, yard, body.bay_id, None)
    if body.inspector_id is not None:
        _, inspector = _resolve_targets(db, yard, None, body.inspector_id)

    start = body.scheduled_at
    end = _end_time(start, body.scheduled_end,
                    int((insp.scheduled_end - insp.scheduled_at).total_seconds() // 60))
    required = sched.split_certs(insp.required_certs) or {"normal"}

    hard = sched.find_hard_conflicts(
        db, yard_id=yard.id, bay=bay, inspector=inspector,
        start=start, end=end, required_certs=list(required),
        exclude_inspection_id=insp.id)
    if hard:
        raise BizError("拖拽落点存在冲突，排期未移动，请改拖其他时段或使用「改派」做链式重排。",
                       code="schedule_conflict", status_code=409, details=hard)
    warnings = _check_window_or_raise(yard, start, end, body.window_confirmed, body.window_reason)

    old = {"from_at": insp.scheduled_at.isoformat(timespec="minutes"),
           "from_end": insp.scheduled_end.isoformat(timespec="minutes"),
           "from_bay": insp.bay_ref.name if insp.bay_ref else insp.bay,
           "from_inspector": insp.inspector.display_name if insp.inspector else None}
    insp.scheduled_at = start
    insp.scheduled_end = end
    if body.bay_id is not None:
        insp.bay_id = bay.id
        insp.bay = bay.name or bay.code
    if body.inspector_id is not None:
        insp.inspector_id = inspector.id
    insp.version += 1
    _add_log(db, insp, user, "move", body.window_reason if warnings else "",
             {**old, "to_at": start.isoformat(timespec="minutes"),
              "to_end": end.isoformat(timespec="minutes"),
              "to_bay": bay.name if bay else None,
              "to_inspector": inspector.display_name if inspector else None})
    for w in warnings:
        _add_log(db, insp, user, "window_override", body.window_reason, w)
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp), "window_warnings": warnings}


# ---------- 改派：车故障 / 人请假（链式重排，预览 + 确认） ----------

def _build_reassign_plan(db, user, insp, body: ReassignPreviewIn):
    if insp.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED):
        raise BizError("该报关单已放行/已结关，不允许改派其查验排期。",
                       code="inspection_locked")
    if insp.status not in ("scheduled", "inspecting"):
        raise BizError(f"排期状态为「{insp.status}」，不能改派。", code="inspection_not_movable")
    yard = insp.yard
    if not yard:
        raise BizError("该排期未挂监管场站，无法在同场站链式重排", code="no_yard")

    forbidden_bays = {body.disable_bay_id} if body.disable_bay_id else set()
    forbidden_insp = {body.disable_inspector_id} if body.disable_inspector_id else set()
    # 故障/请假且未显式指定新资源时，把原资源也加入禁用，避免又排回坏车/请假的人
    if body.reason_type == "bay_broken" and body.new_bay_id is None:
        forbidden_bays.add(insp.bay_id)
    if body.reason_type == "inspector_leave" and body.new_inspector_id is None:
        forbidden_insp.add(insp.inspector_id)
    forbidden_bays.discard(None)
    forbidden_insp.discard(None)

    new_bay, new_inspector = _resolve_targets(
        db, yard, body.new_bay_id, body.new_inspector_id)
    if new_bay is None:
        # 自动选一个资质匹配且未禁用的车位（最终可行性仍由推演引擎裁定）
        req = sched.split_certs(insp.required_certs) or {"normal"}
        new_bay = next((b for b in yard.bays
                        if not b.out_of_service and b.id not in forbidden_bays
                        and req <= sched.split_certs(b.cert_tags)), None)
        if new_bay is None:
            raise BizError("该场站没有可用且资质匹配的车位，无法完成车故障改派。",
                           code="no_alternative_bay")
    if new_inspector is None:
        req = sched.split_certs(insp.required_certs) or {"normal"}
        new_inspector = next((u for u in yard.inspectors
                              if u.available and u.id not in forbidden_insp
                              and req <= sched.split_certs(u.inspector_certs)), None)
        if new_inspector is None:
            raise BizError("该场站没有可派工且资质匹配的查验员，无法完成请假改派。",
                           code="no_alternative_inspector")

    plan = sched.plan_reassign_chain(
        db, anchor=insp, new_start=body.new_scheduled_at,
        new_bay=new_bay, new_inspector=new_inspector,
        duration_minutes=max(15, body.duration_minutes),
        forbidden_bay_ids=forbidden_bays, forbidden_inspector_ids=forbidden_insp)
    return plan, yard, new_bay, new_inspector, forbidden_bays, forbidden_insp


@router.post("/inspections/{insp_id}/reassign/preview")
def reassign_preview(insp_id: int, body: ReassignPreviewIn,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    _require_plan_role(user)
    insp = _load_inspection(db, insp_id, user)
    plan, yard, bay, inspector, fb, fi = _build_reassign_plan(db, user, insp, body)
    if not plan["ok"]:
        raise BizError("改派目标仍有硬冲突，无法生成链式方案。",
                       code="schedule_conflict", status_code=409, details=plan["conflicts"])
    return {"ok": True, "plan": plan,
            "context": {"reason_type": body.reason_type, "reason": body.reason,
                        "yard": yard.name,
                        "disabled_bay_ids": sorted(fb), "disabled_inspector_ids": sorted(fi)}}


@router.post("/inspections/{insp_id}/reassign/confirm")
def reassign_confirm(insp_id: int, body: ReassignConfirmIn,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    _require_plan_role(user)
    insp = _load_inspection(db, insp_id, user)
    # 预览入参结构一致，复用推演
    preview_body = ReassignPreviewIn(
        new_scheduled_at=body.new_scheduled_at, new_bay_id=body.new_bay_id,
        new_inspector_id=body.new_inspector_id, duration_minutes=body.duration_minutes,
        reason_type=body.reason_type, reason=body.reason,
        disable_bay_id=body.disable_bay_id, disable_inspector_id=body.disable_inspector_id)
    plan, yard, bay, inspector, fb, fi = _build_reassign_plan(db, user, insp, preview_body)
    if not plan["ok"]:
        raise BizError("确认时复检发现硬冲突（可能他人刚调整了排期），本次改派未执行，请刷新后重试。",
                       code="schedule_conflict", status_code=409, details=plan["conflicts"])

    batch_id = sched.new_batch_id()
    # 按方案落库：服务端以 moves 为准（不信任客户端回传）
    by_id = {m["inspection_id"]: m for m in plan["moves"]}
    anchor_move = by_id[insp.id]
    for target_insp in db.query(Inspection).filter(
            Inspection.id.in_(list(by_id.keys()))).all():
        m = by_id[target_insp.id]
        target_insp.scheduled_at = datetime.fromisoformat(m["to_scheduled_at"])
        target_insp.scheduled_end = datetime.fromisoformat(m["to_scheduled_end"])
        target_insp.bay_id = m["to_bay_id"]
        target_insp.bay = m["to_bay_name"] or ""
        target_insp.inspector_id = m["to_inspector_id"]
        target_insp.version += 1
        target_insp.reassign_batch_id = batch_id
        if m["is_anchor"]:
            _add_log(db, target_insp, user, "reassign", body.reason,
                     {"reason_type": body.reason_type, **m}, batch_id)
        else:
            _add_log(db, target_insp, user, "chain_move",
                     f"受 {insp.declaration.decl_no} 改派链影响顺延（批次 {batch_id}）",
                     m, batch_id)

    if body.mark_resource_unavailable:
        if body.reason_type == "bay_broken" and body.disable_bay_id:
            broken = db.get(Bay, body.disable_bay_id)
            if broken:
                broken.out_of_service = True
                broken.out_of_service_reason = body.reason
        elif body.reason_type == "inspector_leave" and body.disable_inspector_id:
            leaver = db.get(User, body.disable_inspector_id)
            if leaver:
                leaver.available = False
                leaver.unavailable_reason = body.reason

    db.commit()
    return {"ok": True, "batch_id": batch_id, "plan": plan,
            "hint": f"改派完成：当前单已解绑重排，后续 {len(plan['moves']) - 1} 单链式顺延；"
                    f"{len(plan['blocked'])} 单无法自动顺延需人工处理，"
                    f"已放行/已结关单 {len(plan['skipped_terminal'])} 单未动。"}


# ---------- 取消 / 开始 / 登记结果 ----------

@router.post("/inspections/{insp_id}/cancel")
def cancel_inspection(insp_id: int, body: InspectionCancelIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    _require_plan_role(user)
    insp = _load_inspection(db, insp_id, user)
    if insp.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED):
        raise BizError("报关单已放行/已结关，查验记录不可取消。", code="inspection_locked")
    if insp.status == "cancelled":
        raise BizError("该排期已是取消状态，无需重复取消（幂等拦截）。", code="already_cancelled")
    if insp.status == "done":
        raise BizError("已完成的查验不能取消（结果已记录），如需更正请走查验异常流程。",
                       code="already_done")
    insp.status = "cancelled"
    insp.version += 1
    _add_log(db, insp, user, "cancel", body.reason,
             {"was_status": "scheduled", "at": insp.scheduled_at.isoformat(timespec="minutes")})
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp)}


@router.post("/inspections/{insp_id}/start")
def start_inspection(insp_id: int,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    insp = _load_inspection(db, insp_id, user)
    if user.role not in (Role.CUSTOMS, Role.SUPERVISOR) and \
            not (user.role == Role.INSPECTOR and insp.inspector_id == user.id):
        raise BizError("仅海关、监管员或被派工的查验员本人可开始查验",
                       code="role_forbidden", status_code=403)
    if insp.status != "scheduled":
        raise BizError(f"排期状态为「{insp.status}」，不能开始查验", code="bad_status")
    insp.status = "inspecting"
    insp.version += 1
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp)}


@router.post("/inspections/{insp_id}/finish")
def finish_inspection(insp_id: int, body: InspectionFinishIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    insp = _load_inspection(db, insp_id, user)
    if user.role not in (Role.CUSTOMS, Role.SUPERVISOR) and \
            not (user.role == Role.INSPECTOR and insp.inspector_id == user.id):
        raise BizError("仅海关审单员、监管员或被派工的查验员本人可登记查验结果",
                       code="role_forbidden", status_code=403)
    if insp.status not in ("scheduled", "inspecting"):
        raise BizError(f"排期状态为「{insp.status}」，不能登记结果", code="bad_status")
    insp.status = "abnormal" if body.abnormal else "done"
    insp.result_note = (body.result or "").strip()[:300]
    insp.finished_at = datetime.utcnow()
    insp.version += 1
    _add_log(db, insp, user, "finish", insp.result_note,
             {"abnormal": body.abnormal, "finished_at": insp.finished_at.isoformat(timespec="minutes")})
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp),
            "hint": "查验结果已登记，可在报关单上执行放行或转重审。"}


# ---------- 查验及时率（双口径） ----------

TIME_FORMULA = "实际查验完成日 ≤ 应查验日(due_at) 的单数 ÷ 当月应查验单数（已取消排期不计入）"
VOLUME_FORMULA = "当月已完成查验单数（含异常）÷ 当月派出的查验排期单数（含已取消）"


def _month_bounds(month: str | None) -> tuple[datetime, datetime, str]:
    if month:
        try:
            y, m = month.split("-")
            start = datetime(int(y), int(m), 1)
        except Exception:
            raise BizError("month 需为 YYYY-MM", code="bad_month", status_code=400)
    else:
        n = datetime.utcnow()
        start = datetime(n.year, n.month, 1)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start, end, start.strftime("%Y-%m")


def _timeliness(db: Session, start: datetime, end: datetime, enterprise_id: int | None) -> dict:
    q = db.query(Inspection)
    if enterprise_id:
        q = q.join(Declaration).filter(Declaration.enterprise_id == enterprise_id)
    rows = q.all()

    # 口径 A（时效）：应查验日 due_at 落在本月、未取消
    due_rows = [r for r in rows if r.due_at and start <= r.due_at < end and r.status != "cancelled"]
    on_time = [r for r in due_rows if r.finished_at is not None and r.finished_at <= r.due_at]
    late = [r for r in due_rows if not (r.finished_at is not None and r.finished_at <= r.due_at)]

    # 口径 B（单量）：当月派出（created_at 落在本月），完成含异常；取消单仍在分母
    dispatched = [r for r in rows if start <= r.created_at < end]
    finished = [r for r in dispatched if r.status in ("done", "abnormal")]
    cancelled = [r for r in dispatched if r.status == "cancelled"]

    reassigns = db.query(ScheduleLog).filter(
        ScheduleLog.action.in_(("reassign", "chain_move")),
        ScheduleLog.created_at >= start, ScheduleLog.created_at < end).count()

    def rate(num, den):
        return round(num * 1000 / den) / 10 if den else None  # 百分比保留 1 位

    return {
        "time": {
            "key": "time", "name": "时效口径（实际查验日 / 应查验日）",
            "formula": TIME_FORMULA,
            "numerator": len(on_time), "denominator": len(due_rows),
            "rate_percent": rate(len(on_time), len(due_rows)),
            "late_or_pending": len(late),
        },
        "volume": {
            "key": "volume", "name": "单量口径（已完成查验单 / 派单）",
            "formula": VOLUME_FORMULA,
            "numerator": len(finished), "denominator": len(dispatched),
            "rate_percent": rate(len(finished), len(dispatched)),
            "cancelled": len(cancelled),
        },
        "reassign_logs": reassigns,
    }


@router.get("/metrics/timeliness")
def timeliness(month: str | None = None,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    enterprise_id = user.enterprise_id if user.role == Role.ENTERPRISE else None
    start, end, key = _month_bounds(month)
    current = _timeliness(db, start, end, enterprise_id)

    # 近 6 个月双口径走势：频繁改派月份两口径走势背离可直观看到
    months = []
    for back in range(5, -1, -1):
        y = start.year
        m = start.month - back
        while m <= 0:
            m += 12
            y -= 1
        ms = datetime(y, m, 1)
        me = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        t = _timeliness(db, ms, me, enterprise_id)
        months.append({"month": ms.strftime("%Y-%m"),
                       "time_rate": t["time"]["rate_percent"],
                       "volume_rate": t["volume"]["rate_percent"],
                       "reassign_logs": t["reassign_logs"]})
    return {"month": key, "calibers": current, "months": months,
            "notice": "两个口径回答不同问题：时效口径衡量「是否按应查验日完成」，"
                      "频繁改派会把实际查验日推后、该口径走低；单量口径衡量「派出的单最终做了多少」，"
                      "改派后只要完成仍计入，该口径可能保持高位。考核以哪个为准请在界面切换查看，勿混用。"}
