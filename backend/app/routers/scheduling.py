"""查验排期路由：场站资源、周日甘特、安排查验、拖拽改期、改派链式重排、取消、及时率。

所有写操作都遵循同一套冲突协议：
- 阻断级冲突（车位/查验员双占、资质不匹配、故障/请假、过去时间）→ 409 scheduling_conflict，
  响应体带 conflicts[] 结构化清单，前端逐条标红、点开看原因；
- 警告级（超营业窗口、普货占专用台位）→ 未带 confirm 时 409 need_confirm；
  前端二次确认并填写原因后带 confirm=true 重提，原因写入 schedule_changes 留痕。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    User, Role, Yard, Bay, Inspection, Declaration, ScheduleChange,
    DeclStatus, BizError, BAY_KIND_LABELS, QUAL_LABELS, QUAL_TO_BAY_KIND,
    STATUS_LABELS, guard_transition,
)
from ..deps import get_current_user
from ..schemas import (
    InspectionScheduleIn, InspectionRescheduleIn, ReassignIn, InspectionCancelIn,
    InspectionFinishIn, inspection_out, user_out,
)
from .. import scheduling as sch
from ..scheduling import (
    check_slot, blocking, plan_chain_reassign, log_change, end_at,
    in_business_window, clamp_to_window,
)

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

CHANGE_LABELS = {
    "schedule": "安排查验",
    "drag": "拖拽改期",
    "out_of_window": "超窗口落位",
    "reassign": "改派",
    "chain_shift": "链式顺延",
    "cancel": "取消排期",
    "bay_broken": "车位故障登记",
    "inspector_leave": "查验员请假登记",
}


def require_scheduling_user():
    """可写排期的角色：海关审单员（审单布控）、监管员（统筹）。"""
    from ..deps import require_roles
    return require_roles(Role.CUSTOMS, Role.SUPERVISOR)


# ---------- 资源目录 ----------

@router.get("/resources")
def resources(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    yards = db.query(Yard).filter(Yard.active == True).order_by(Yard.id).all()
    inspectors = db.query(User).filter(User.role == Role.INSPECTOR, User.active == True).order_by(User.id).all()
    return {
        "yards": [{
            "id": y.id,
            "name": y.name,
            "port": y.port,
            "open_hour": float(y.open_hour),
            "close_hour": float(y.close_hour),
            "work_weekend": y.work_weekend,
            "bays": [{
                "id": b.id, "code": b.code, "name": b.name,
                "kind": b.kind, "kind_label": BAY_KIND_LABELS.get(b.kind, b.kind),
                "out_of_service": b.out_of_service, "note": b.note,
                "sort_no": b.sort_no,
            } for b in y.bays],
        } for y in yards],
        "inspectors": [user_out(u) for u in inspectors],
        "qual_labels": QUAL_LABELS,
        "bay_kinds": BAY_KIND_LABELS,
    }


def _load_yard_bay_inspector(db, yard_id, bay_id, inspector_id) -> tuple[Yard, Bay, User]:
    yard = db.get(Yard, yard_id)
    bay = db.get(Bay, bay_id)
    inspector = db.get(User, inspector_id)
    if not yard or not bay or bay.yard_id != yard.id:
        raise BizError("场站或车位不存在/不属于该场站，请刷新资源目录后重选",
                       code="yard_bay_not_found", status_code=404)
    if not inspector or inspector.role != Role.INSPECTOR:
        raise BizError("查验员不存在或不是查验员账号", code="inspector_not_found", status_code=404)
    return yard, bay, inspector


def _conflict_error(conflicts, code="scheduling_conflict"):
    return BizError(
        "该落位存在排期冲突，未做任何变更。请按逐条红色提示调整后再提交，点开每条可查看具体原因。",
        code=code, status_code=409,
        extra={"conflicts": [c.to_dict() for c in conflicts]},
    )


# ---------- 甘特日历 ----------

@router.get("/calendar")
def calendar(yard_id: int, start: str, end: str,
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    yard = db.get(Yard, yard_id)
    if not yard:
        raise BizError("监管场站不存在", code="yard_not_found", status_code=404)
    try:
        dt_start = datetime.fromisoformat(start)
        dt_end = datetime.fromisoformat(end)
    except ValueError:
        raise BizError("时间范围格式错误", code="bad_time_range", status_code=400)

    q = db.query(Inspection).filter(
        Inspection.yard_id == yard_id,
        Inspection.scheduled_at >= dt_start - timedelta(hours=12),
        Inspection.scheduled_at <= dt_end + timedelta(hours=12),
    )
    if user.role == Role.ENTERPRISE:
        q = q.join(Declaration).filter(Declaration.enterprise_id == user.enterprise_id)
    rows = q.order_by(Inspection.scheduled_at).all()

    active = [i for i in rows if i.status != "cancelled"]
    cancelled = [i for i in rows if i.status == "cancelled"]

    # 为每条有效排期体检：车位故障 / 查验员请假 / 双占 / 资质漂移 → 前端逐条标红、点开看原因
    def with_conflicts(i: Inspection) -> dict:
        item = inspection_out(i)
        current = []
        if i.bay and i.inspector:
            current = [c.to_dict() for c in check_slot(
                db, decl=i.declaration, yard=i.yard, bay=i.bay, inspector=i.inspector,
                start=i.scheduled_at, minutes=i.duration_minutes or 120, ignore_id=i.id)
                if c.code != "scheduled_in_past"]  # 存量任务过去时间用 overdue 表达，不当落位冲突
        # 逾期未完成单独提示（非阻断冲突）
        overdue = i.status in ("pending", "inspecting") and i.scheduled_at < datetime.utcnow()
        item["current_conflicts"] = current
        item["has_conflict"] = bool(current)
        item["overdue"] = overdue
        return item

    active_items = [with_conflicts(i) for i in active]

    # 落在窗内的工作日/休息日日历格，供前端画暗色非作业时段（区间左闭右开：[d0, d1)）
    d0, d1 = dt_start.date(), dt_end.date()
    days = []
    cur = d0
    while cur < d1:
        days.append({
            "date": cur.isoformat(),
            "weekend": cur.weekday() >= 5,
            "working": yard.work_weekend or cur.weekday() < 5,
        })
        cur += timedelta(days=1)

    return {
        "yard": {
            "id": yard.id, "name": yard.name, "port": yard.port,
            "open_hour": float(yard.open_hour), "close_hour": float(yard.close_hour),
            "work_weekend": yard.work_weekend,
        },
        "range": {"start": dt_start.isoformat(), "end": dt_end.isoformat()},
        "days": days,
        "inspections": active_items,
        "cancelled_count": len(cancelled),
        "cancelled": [inspection_out(i) for i in cancelled],
        "all_cancelled": len(active) == 0 and len(cancelled) > 0,
    }


# ---------- 预览冲突（不落库，安排表单实时校验用） ----------

@router.post("/inspections/preview")
def preview_inspection(body: InspectionScheduleIn,
                       user: User = Depends(require_scheduling_user()),
                       db: Session = Depends(get_db)):
    decl = db.get(Declaration, body.declaration_id)
    if not decl:
        raise BizError("报关单不存在", code="not_found", status_code=404)
    yard, bay, inspector = _load_yard_bay_inspector(db, body.yard_id, body.bay_id, body.inspector_id)
    conflicts = check_slot(
        db, decl=decl, yard=yard, bay=bay, inspector=inspector,
        start=body.scheduled_at, minutes=body.duration_minutes)
    return {
        "ok": True,
        "required_qual": sch.required_qual(decl),
        "required_qual_label": QUAL_LABELS.get(sch.required_qual(decl), ""),
        "conflicts": [c.to_dict() for c in conflicts],
        "blocking": [c.to_dict() for c in blocking(conflicts)],
        "need_confirm": any(c.severity == "warning" for c in conflicts) and not blocking(conflicts),
        "suggested_start": clamp_to_window(yard, body.scheduled_at, body.duration_minutes).isoformat(timespec="minutes"),
    }


# ---------- 安排查验（审单逻辑审核通过 → 布控 → 排期） ----------

@router.post("/inspections")
def schedule_inspection(body: InspectionScheduleIn,
                        user: User = Depends(require_scheduling_user()),
                        db: Session = Depends(get_db)):
    decl = db.get(Declaration, body.declaration_id)
    if not decl:
        raise BizError("报关单不存在", code="not_found", status_code=404)
    if decl.status not in (DeclStatus.REVIEWING, DeclStatus.INSPECTING):
        raise BizError(
            f"报关单当前为「{decl.status.value}」，只有审单审核通过布控后才能安排查验。",
            code="bad_status_for_inspection")
    # 已在查验中的单：同一时刻只允许一条有效排期（改走改派/改期，不重复占用资源）
    existing = [i for i in decl.inspections if i.status in ("pending", "inspecting")]
    if existing:
        raise BizError(
            f"该单已有待查验排期（{existing[0].scheduled_at:%Y-%m-%d %H:%M} {existing[0].bay.code if existing[0].bay else ''}）。"
            "调整请用拖拽改期或改派，不能重复排期占用车位/查验员。",
            code="inspection_already_scheduled")

    yard, bay, inspector = _load_yard_bay_inspector(db, body.yard_id, body.bay_id, body.inspector_id)
    conflicts = check_slot(
        db, decl=decl, yard=yard, bay=bay, inspector=inspector,
        start=body.scheduled_at, minutes=body.duration_minutes)
    hard = blocking(conflicts)
    warns = [c for c in conflicts if c.severity == "warning"]
    if hard:
        raise _conflict_error(conflicts)
    if warns and not body.confirm:
        raise BizError(
            "拟落位时间在场站营业窗口外（或占用专用台位），须二次确认并填写非常规安排原因后才能落位。",
            code="need_confirm", status_code=409,
            extra={"conflicts": [c.to_dict() for c in warns]})

    # 审单中 → 查验中（状态机：非法回退/越权在此拦截）
    moved = False
    if decl.status == DeclStatus.REVIEWING:
        guard_transition(decl.status, DeclStatus.INSPECTING, user.role)
        decl.status = DeclStatus.INSPECTING
        decl.version += 1
        if decl.customs_officer_id is None and user.role == Role.CUSTOMS:
            decl.customs_officer_id = user.id
        moved = True

    now = datetime.utcnow()
    insp = Inspection(
        declaration_id=decl.id, yard_id=yard.id, bay_id=bay.id, inspector_id=inspector.id,
        scheduled_at=body.scheduled_at,
        scheduled_end=end_at(body.scheduled_at, body.duration_minutes),
        duration_minutes=body.duration_minutes,
        required_qual=sch.required_qual(decl),
        due_date=body.scheduled_at,  # 应查验日 = 布控排期确定的计划日，后续改派只动计划不动应查日
        status="pending",
    )
    db.add(insp)
    db.flush()
    from ..routers.declarations import _add_event
    if moved:
        _add_event(db, decl, user, DeclStatus.REVIEWING, DeclStatus.INSPECTING,
                   f"审单逻辑审核通过，布控查验并安排至{yard.name} {bay.code} "
                   f"{body.scheduled_at:%Y-%m-%d %H:%M}，查验员 {inspector.display_name}")
    ctype = "out_of_window" if any(c.code in ("out_of_window", "cross_window") for c in warns) else "schedule"
    log_change(db, inspection=insp, actor=user, ctype=ctype,
               reason=body.reason or ("；".join(c.title for c in warns) if warns else "审单布控，安排查验"),
               detail={
                   "action": "create",
                   "scheduled_at": insp.scheduled_at.isoformat(timespec="minutes"),
                   "scheduled_end": insp.scheduled_end.isoformat(timespec="minutes"),
                   "yard": yard.name, "bay": bay.code, "inspector": inspector.display_name,
                   "warnings": [c.to_dict() for c in warns],
               })
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp),
            "declaration_status": decl.status.value}


# ---------- 甘特拖拽改期 ----------

@router.post("/inspections/{insp_id}/reschedule")
def reschedule(insp_id: int, body: InspectionRescheduleIn,
               user: User = Depends(require_scheduling_user()),
               db: Session = Depends(get_db)):
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError("查验排期不存在", code="not_found", status_code=404)
    if insp.status not in ("pending", "inspecting"):
        raise BizError(f"该查验任务已{insp.status}，不能再拖拽改期", code="inspection_not_movable")
    if insp.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED, DeclStatus.CANCELLED):
        raise BizError(
            f"报关单 {insp.declaration.decl_no} 已{STATUS_LABELS[insp.declaration.status]}，查验时间锁定，不能改期。",
            code="declaration_locked")

    yard = insp.yard
    bay = db.get(Bay, body.bay_id) if body.bay_id else insp.bay
    inspector = db.get(User, body.inspector_id) if body.inspector_id else insp.inspector
    if not bay or bay.yard_id != yard.id:
        raise BizError("目标车位不属于该监管场站", code="bay_wrong_yard", status_code=400)
    if not inspector or inspector.role != Role.INSPECTOR:
        raise BizError("目标查验员无效", code="inspector_not_found", status_code=400)

    old_start, old_end, old_bay, old_insp = insp.scheduled_at, insp.end_time, insp.bay, insp.inspector
    conflicts = check_slot(
        db, decl=insp.declaration, yard=yard, bay=bay, inspector=inspector,
        start=body.scheduled_at, minutes=insp.duration_minutes, ignore_id=insp.id)
    hard = blocking(conflicts)
    warns = [c for c in conflicts if c.severity == "warning"]
    if hard:
        raise _conflict_error(conflicts)
    if warns and not body.confirm:
        raise BizError(
            "拖拽落点超出该场站营业窗口（或为普货占用专用台位），须二次确认并填写原因后落位，系统将留痕。",
            code="need_confirm", status_code=409,
            extra={"conflicts": [c.to_dict() for c in warns]})
    if body.confirm and any(c.code in ("out_of_window", "cross_window") for c in warns) and len(body.reason) < 4:
        raise BizError("超窗口落位必须填写不少于 4 个字的原因（留痕要求）", code="reason_required")

    insp.scheduled_at = body.scheduled_at
    insp.scheduled_end = end_at(body.scheduled_at, insp.duration_minutes)
    insp.bay_id = bay.id
    insp.inspector_id = inspector.id
    insp.reassign_count += 1
    ctype = "out_of_window" if any(c.code in ("out_of_window", "cross_window") for c in warns) else "drag"
    log_change(db, inspection=insp, actor=user, ctype=ctype, reason=body.reason or "甘特拖拽改期",
               detail={
                   "old": {"start": old_start.isoformat(timespec="minutes"),
                           "end": old_end.isoformat(timespec="minutes"),
                           "bay": old_bay.code if old_bay else None,
                           "inspector": old_insp.display_name if old_insp else None},
                   "new": {"start": insp.scheduled_at.isoformat(timespec="minutes"),
                           "end": insp.scheduled_end.isoformat(timespec="minutes"),
                           "bay": bay.code, "inspector": inspector.display_name},
                   "warnings": [c.to_dict() for c in warns],
               })
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp)}


# ---------- 改派（车故障 / 人请假）+ 链式重排 ----------

@router.post("/inspections/{insp_id}/reassign")
def reassign(insp_id: int, body: ReassignIn,
             user: User = Depends(require_scheduling_user()),
             db: Session = Depends(get_db)):
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError("查验排期不存在", code="not_found", status_code=404)
    if insp.status not in ("pending", "inspecting"):
        raise BizError(f"该查验任务已{insp.status}，不能改派", code="inspection_not_movable")
    if insp.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED, DeclStatus.CANCELLED):
        raise BizError(
            f"报关单 {insp.declaration.decl_no} 已{STATUS_LABELS[insp.declaration.status]}，不允许改派（终态锁定）。",
            code="declaration_locked")

    yard = insp.yard
    new_bay = db.get(Bay, body.new_bay_id) if body.new_bay_id else insp.bay
    new_inspector = db.get(User, body.new_inspector_id) if body.new_inspector_id else insp.inspector
    if not new_bay or new_bay.yard_id != yard.id:
        raise BizError("新车位不属于该监管场站（改派不跨场站，需跨场站请取消后重新安排）",
                       code="bay_wrong_yard", status_code=400)
    if not new_inspector or new_inspector.role != Role.INSPECTOR:
        raise BizError("新查验员无效", code="inspector_not_found", status_code=400)
    new_start = body.new_scheduled_at or insp.scheduled_at

    # 目标单新槽位自身的冲突
    conflicts = check_slot(
        db, decl=insp.declaration, yard=yard, bay=new_bay, inspector=new_inspector,
        start=new_start, minutes=insp.duration_minutes, ignore_id=insp.id)
    hard = blocking(conflicts)
    warns = [c for c in conflicts if c.severity == "warning"]
    if hard:
        raise _conflict_error(conflicts)

    # 链式重排预演
    plan = plan_chain_reassign(
        db, target=insp, yard=yard, new_bay=new_bay, new_inspector=new_inspector,
        new_start=new_start, minutes=insp.duration_minutes)
    chain_shifts = [s for s in plan["shifts"] if s.inspection_id != insp.id and s.old_start != s.new_start]

    if not body.confirm:
        # dry-run：返回完整预演给前端确认
        return {
            "ok": True,
            "dry_run": True,
            "target": inspection_out(insp),
            "new_slot": {
                "bay": new_bay.code, "bay_kind": new_bay.kind,
                "inspector": new_inspector.display_name,
                "scheduled_at": new_start.isoformat(timespec="minutes"),
                "scheduled_end": end_at(new_start, insp.duration_minutes).isoformat(timespec="minutes"),
            },
            "conflicts": [c.to_dict() for c in conflicts],
            "need_confirm_window": any(c.code in ("out_of_window", "cross_window") for c in warns),
            "chain": [s.to_dict() for s in plan["shifts"]],
            "chain_moved_count": len(chain_shifts),
            "locked_barriers": plan["locked_barriers"],
        }

    # —— 确认执行 ——
    if any(c.code in ("out_of_window", "cross_window") for c in warns) and len(body.reason) < 4:
        raise BizError("目标时间超营业窗口，改派原因中必须说明非常规时段安排理由", code="reason_required")

    batch = f"CHAIN-{insp.id}-{datetime.utcnow():%Y%m%d%H%M%S}"
    proposed = plan["proposed"]

    # 目标单解绑旧资源、绑定新资源
    old = {"bay": insp.bay.code if insp.bay else "", "inspector": insp.inspector.display_name if insp.inspector else "",
           "start": insp.scheduled_at.isoformat(timespec="minutes")}
    insp.bay_id = new_bay.id
    insp.inspector_id = new_inspector.id
    insp.scheduled_at = new_start
    insp.scheduled_end = end_at(new_start, insp.duration_minutes)
    insp.reassign_count += 1
    log_change(db, inspection=insp, actor=user, ctype="reassign", reason=body.reason,
               chain_batch=batch,
               detail={"old": old,
                       "new": {"bay": new_bay.code, "inspector": new_inspector.display_name,
                               "start": insp.scheduled_at.isoformat(timespec="minutes")},
                       "chain_moved_count": len(chain_shifts)})

    # 链条后续单：只后移、不动已放行/已结关/已完成（plan 已把它们排除在 movable 外）
    for s in plan["shifts"]:
        if s.inspection_id == insp.id or s.old_start == s.new_start:
            continue
        other = db.get(Inspection, s.inspection_id)
        if other.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED, DeclStatus.CANCELLED) \
                or other.status in ("done", "cancelled"):
            # 双保险：终态单绝不写入
            continue
        p = proposed[other.id]
        other.scheduled_at = p["start"]
        other.scheduled_end = p["end"]
        other.reassign_count += 1
        log_change(db, inspection=other, actor=user, ctype="chain_shift",
                   reason=f"因 {insp.declaration.decl_no} 改派（{body.reason}）触发链式顺延",
                   chain_batch=batch,
                   detail={"old": s.old_start.isoformat(timespec="minutes"),
                           "new": s.new_start.isoformat(timespec="minutes"),
                           "caused_by_decl": insp.declaration.decl_no,
                           "shift_reasons": s.reasons})

    db.commit()
    return {
        "ok": True,
        "dry_run": False,
        "chain_batch": batch,
        "target": inspection_out(insp),
        "chain_moved_count": len(chain_shifts),
        "chain": [s.to_dict() for s in plan["shifts"]],
        "locked_barriers": plan["locked_barriers"],
    }


# ---------- 取消排期 ----------

@router.post("/inspections/{insp_id}/cancel")
def cancel_inspection(insp_id: int, body: InspectionCancelIn,
                      user: User = Depends(require_scheduling_user()),
                      db: Session = Depends(get_db)):
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError("查验排期不存在", code="not_found", status_code=404)
    if insp.status not in ("pending", "inspecting"):
        raise BizError(f"查验任务当前为「{insp.status}」，无需取消", code="inspection_not_cancellable")
    insp.status = "cancelled"
    insp.cancel_reason = body.reason
    log_change(db, inspection=insp, actor=user, ctype="cancel", reason=body.reason,
               detail={"scheduled_at": insp.scheduled_at.isoformat(timespec="minutes"),
                       "bay": insp.bay.code if insp.bay else None,
                       "inspector": insp.inspector.display_name if insp.inspector else None})
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp)}


# ---------- 开始 / 完成查验 ----------

@router.post("/inspections/{insp_id}/start")
def start_inspection(insp_id: int,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError("查验排期不存在", code="not_found", status_code=404)
    if user.role == Role.ENTERPRISE:
        raise BizError("企业账号无权登记查验", code="role_forbidden", status_code=403)
    if insp.status != "pending":
        raise BizError("只有待查验任务可以开始", code="bad_inspection_status")
    insp.status = "inspecting"
    insp.started_at = datetime.utcnow()
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp)}


@router.post("/inspections/{insp_id}/finish")
def finish_inspection(insp_id: int, payload: InspectionFinishIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    insp = db.get(Inspection, insp_id)
    if not insp:
        raise BizError("查验排期不存在", code="not_found", status_code=404)
    if user.role not in (Role.CUSTOMS, Role.INSPECTOR, Role.SUPERVISOR):
        raise BizError("仅海关审单员/查验员可登记查验结果", code="role_forbidden", status_code=403)
    if insp.status not in ("inspecting", "pending"):
        raise BizError(f"查验任务当前为「{insp.status}」，不能重复登记结果", code="bad_inspection_status")
    abnormal = payload.abnormal
    insp.status = "abnormal" if abnormal else "done"
    insp.result_note = (payload.result or "查验无误")[:300]
    insp.finished_at = datetime.utcnow()
    if not insp.started_at:
        insp.started_at = insp.finished_at
    on_time = insp.due_date and insp.finished_at.date() <= insp.due_date.date()
    log_change(db, inspection=insp, actor=user,
               ctype="schedule", reason="查验结果登记",
               detail={"result": insp.result_note, "abnormal": abnormal,
                       "finished_at": insp.finished_at.isoformat(timespec="minutes"),
                       "due_date": insp.due_date.isoformat(timespec="minutes") if insp.due_date else None,
                       "on_time": bool(on_time)})
    db.commit()
    db.refresh(insp)
    return {"ok": True, "inspection": inspection_out(insp),
            "hint": "查验结果已登记，可在报关单上执行放行或转重审。"}


# ---------- 故障 / 请假 登记（改派的触发源，同样留痕） ----------

@router.post("/bays/{bay_id}/status")
def set_bay_status(bay_id: int, payload: dict,
                   user: User = Depends(require_scheduling_user()),
                   db: Session = Depends(get_db)):
    bay = db.get(Bay, bay_id)
    if not bay:
        raise BizError("车位不存在", code="not_found", status_code=404)
    broken = bool(payload.get("out_of_service"))
    reason = str(payload.get("reason", ""))[:200]
    bay.out_of_service = broken
    if reason:
        bay.note = reason
    affected = [i for i in db.query(Inspection).filter(
        Inspection.bay_id == bay.id, Inspection.status.in_(("pending", "inspecting"))).all()]
    log_change(db, inspection=None, actor=user,
               ctype="bay_broken", reason=reason or ("车位故障停用" if broken else "车位恢复使用"),
               yard_id=bay.yard_id,
               detail={"bay": bay.code, "out_of_service": broken,
                       "affected_inspection_ids": [i.id for i in affected]})
    db.commit()
    return {"ok": True, "out_of_service": bay.out_of_service,
            "affected_inspection_ids": [i.id for i in affected]}


@router.post("/inspectors/{inspector_id}/status")
def set_inspector_status(inspector_id: int, payload: dict,
                         user: User = Depends(require_scheduling_user()),
                         db: Session = Depends(get_db)):
    inspector = db.get(User, inspector_id)
    if not inspector or inspector.role != Role.INSPECTOR:
        raise BizError("查验员不存在", code="not_found", status_code=404)
    leave = bool(payload.get("on_leave"))
    reason = str(payload.get("reason", ""))[:200]
    inspector.on_leave = leave
    affected = [i for i in db.query(Inspection).filter(
        Inspection.inspector_id == inspector.id, Inspection.status.in_(("pending", "inspecting"))).all()]
    log_change(db, inspection=None, actor=user,
               ctype="inspector_leave", reason=reason or ("查验员请假" if leave else "查验员销假"),
               detail={"inspector": inspector.display_name, "on_leave": leave,
                       "affected_inspection_ids": [i.id for i in affected]})
    db.commit()
    return {"ok": True, "on_leave": inspector.on_leave,
            "affected_inspection_ids": [i.id for i in affected]}


# ---------- 排期变更留痕 ----------

@router.get("/changes")
def list_changes(inspection_id: int | None = None, yard_id: int | None = None,
                 limit: int = 50,
                 user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    q = db.query(ScheduleChange)
    if inspection_id:
        q = q.filter(ScheduleChange.inspection_id == inspection_id)
    if yard_id:
        q = q.filter(ScheduleChange.yard_id == yard_id)
    rows = q.order_by(ScheduleChange.id.desc()).limit(min(limit, 200)).all()
    out = []
    for c in rows:
        try:
            detail = json.loads(c.detail) if c.detail else {}
        except json.JSONDecodeError:
            detail = {}
        out.append({
            "id": c.id,
            "inspection_id": c.inspection_id,
            "declaration_id": c.declaration_id,
            "change_type": c.change_type,
            "change_type_label": CHANGE_LABELS.get(c.change_type, c.change_type),
            "actor_name": c.actor_name,
            "reason": c.reason,
            "detail": detail,
            "chain_batch": c.chain_batch,
            "created_at": c.created_at.isoformat(timespec="seconds"),
        })
    return out


# ---------- 查验及时率（双口径，口径写界面） ----------

def _month_bounds(month: str) -> tuple[datetime, datetime, str]:
    try:
        y, m = map(int, month.split("-"))
        start = datetime(y, m, 1)
    except (ValueError, AttributeError):
        raise BizError("月份格式应为 YYYY-MM", code="bad_month", status_code=400)
    end = datetime(y + (m // 12), (m % 12) + 1, 1)
    return start, end, month


@router.get("/timeliness")
def timeliness(month: str | None = None,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    now = datetime.utcnow()
    if month:
        starts = [_month_bounds(month)[0]]
    else:
        # 默认给最近 6 个月序列（含当月）
        y, m = now.year, now.month
        starts = []
        for k in range(5, -1, -1):
            yy, mm = y, m - k
            while mm <= 0:
                mm += 12
                yy -= 1
            starts.append(datetime(yy, mm, 1))

    def bounds(s):
        e = datetime(s.year + (s.month // 12), (s.month % 12) + 1, 1)
        return s, e

    series = []
    for s in starts:
        ms, me = bounds(s)
        rows = db.query(Inspection).filter(Inspection.created_at >= ms, Inspection.created_at < me).all()
        valid = [i for i in rows if i.status != "cancelled"]
        dispatched = len(valid)
        done_rows = [i for i in valid if i.status == "done"]
        completed_rate = round(len(done_rows) * 100 / dispatched, 1) if dispatched else None

        # 口径A（日口径）：应查验日落在本月的单，实际查验完成日 ≤ 应查验日 记为准时
        due_rows = [i for i in valid if i.due_date and ms <= i.due_date < me]
        on_time = [i for i in due_rows if i.finished_at and i.finished_at.date() <= i.due_date.date()]
        day_rate = round(len(on_time) * 100 / len(due_rows), 1) if due_rows else None
        reassign_total = sum(i.reassign_count or 0 for i in valid)

        series.append({
            "month": f"{s.year}-{s.month:02d}",
            "day_rate": day_rate,                 # 口径一：实际查验日/应查验日
            "completion_rate": completed_rate,    # 口径二：已完成/派单
            "due_count": len(due_rows),
            "on_time_count": len(on_time),
            "dispatched": dispatched,
            "completed": len(done_rows),
            "pending_or_abnormal": dispatched - len(done_rows),
            "reassign_total": reassign_total,
        })

    current = next((x for x in series if x["month"] == month), series[-1] if series else None)
    return {
        "current": current,
        "series": series,
        "definitions": {
            "day_rate": {
                "name": "口径一 · 查验准点率（按日）",
                "formula": "实际查验完成日 ≤ 应查验日 的单数 ÷ 当月应查验单数",
                "denominator": "应查验日（布控排期时确定，改派只动计划日、不回改应查验日）落在本月、且未取消的查验单",
                "numerator": "其中在应查验日当天或之前已完成查验的单数；未完成/逾期完成均不计入",
                "answers": "「承诺的查验时间守不守得住」——频繁改派会把实际查验日推过应查验日，此口径下降",
            },
            "completion_rate": {
                "name": "口径二 · 查验完成率（按单量）",
                "formula": "当月已完成查验单数 ÷ 当月派单数",
                "denominator": "当月派出（创建排期）且未取消的查验任务数",
                "numerator": "其中状态为已完成的任务数",
                "answers": "「派出的查验活干没干完」——只要单最终查完就算完成，不看比原计划晚了几天",
            },
            "divergence_note": "频繁改派的月份两口径可能方向相反：单子最终都查完（完成率高），"
                               "但实际查验日普遍晚于应查验日（准点率低）。单看一个口径会误判，故并列展示。",
        },
    }
