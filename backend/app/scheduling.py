"""查验排期引擎：营业窗口、结构化冲突检测、改派链式重排。

设计原则：
- 冲突「逐条结构化」返回：每条含 code/标题/可读原因/冲突对方，前端逐条标红、可点开看原因，
  绝不只抛一句笼统报错；
- 链式重排只后移不前移、按时间单调推进，数学上不可能形成环形依赖；
- 已放行 / 已结关单的查验记录是「硬屏障」：永不移动，链条撞上就报告冲突交人工决策；
  已完成 / 已取消的排期不占资源。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import (
    User, Bay, Yard, Inspection, Declaration, ScheduleChange,
    DeclStatus, Role, BizError,
    qual_for_cargo, QUAL_LABELS, QUAL_TO_BAY_KIND, BAY_KIND_LABELS,
)

ACTIVE_TASK_STATUSES = ("pending", "inspecting")
LOCKED_DECL_STATUSES = (DeclStatus.RELEASED, DeclStatus.CLOSED, DeclStatus.CANCELLED)
MAX_CHAIN_DAYS = 30  # 链式顺延最远 30 天，超出报 blocked 交人工


# ---------- 时间工具（naive UTC 即排期本地时间，全系统口径一致） ----------

def end_at(start: datetime, minutes: int) -> datetime:
    return start + timedelta(minutes=max(30, minutes))


def overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """半开区间重叠：[s1,e1) 与 [s2,e2)。首尾相接不算冲突。"""
    return s1 < e2 and s2 < e1


def in_business_window(yard: Yard, dt: datetime) -> bool:
    """是否落在场站营业窗口内（含周末作业开关）。"""
    if not yard.work_weekend and dt.weekday() >= 5:
        return False
    h = dt.hour + dt.minute / 60
    return float(yard.open_hour) <= h < float(yard.close_hour)


def next_window_start(yard: Yard, dt: datetime) -> datetime:
    """dt 之后（或当天）最近的开窗时刻，顺延落位用。"""
    base = dt.replace(minute=0, second=0, microsecond=0)
    oh = float(yard.open_hour)
    cand = base.replace(hour=int(oh), minute=int(round((oh - int(oh)) * 60)))
    if cand < dt:
        cand += timedelta(days=1)
    while not yard.work_weekend and cand.weekday() >= 5:
        cand += timedelta(days=1)
    return cand


def clamp_to_window(yard: Yard, start: datetime, minutes: int) -> datetime:
    """把计划开始夹到营业窗口内：窗口外/装不下就挪到下一个开窗时刻。"""
    e = end_at(start, minutes)
    day_end = start.replace(hour=int(float(yard.close_hour)),
                            minute=int(round((float(yard.close_hour) % 1) * 60)),
                            second=0, microsecond=0)
    if in_business_window(yard, start) and e <= day_end and (yard.work_weekend or start.weekday() < 5):
        return start
    nxt = next_window_start(yard, start)
    # 下一个开窗日仍装不下（理论上窗口>=8h、单任务≤8h，不会发生），再推一天
    if end_at(nxt, minutes) > nxt.replace(hour=int(float(yard.close_hour)), minute=0):
        nxt = next_window_start(yard, nxt + timedelta(days=1))
    return nxt


# ---------- 结构化冲突 ----------

@dataclass
class Conflict:
    code: str
    kind: str          # bay / inspector / qual / resource / window / time / locked
    severity: str      # blocking 阻断落位 / warning 需二次确认
    title: str
    reason: str
    conflict_with: dict | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code, "kind": self.kind, "severity": self.severity,
            "title": self.title, "reason": self.reason,
            "conflict_with": self.conflict_with,
        }


def _brief(i: Inspection) -> dict:
    return {
        "inspection_id": i.id,
        "declaration_id": i.declaration_id,
        "decl_no": i.declaration.decl_no,
        "cargo_name": i.declaration.cargo_name,
        "scheduled_at": i.scheduled_at.isoformat(timespec="minutes"),
        "scheduled_end": i.end_time.isoformat(timespec="minutes"),
        "bay_code": i.bay.code if i.bay else None,
        "inspector_name": i.inspector.display_name if i.inspector else None,
        "status": i.status,
        "decl_status": i.declaration.status.value,
    }


def required_qual(decl: Declaration) -> str | None:
    return qual_for_cargo(decl.hs_code, decl.cargo_name)


def check_slot(db: Session, *, decl: Declaration, yard: Yard, bay: Bay, inspector: User,
               start: datetime, minutes: int, ignore_id: int | None = None) -> list[Conflict]:
    """校验一个拟落位时间槽的全部冲突，逐条返回（不抛异常），供安排/拖拽/改派复用。"""
    out: list[Conflict] = []
    finish = end_at(start, minutes)
    need_qual = required_qual(decl)

    # —— 基础资源状态 ——
    if bay.out_of_service:
        out.append(Conflict(
            code="bay_broken", kind="resource", severity="blocking",
            title="车位故障停用",
            reason=f"车位「{bay.code} {bay.name}」当前故障停用（{bay.note or '场站已登记'}），"
                   f"不能安排查验。请改派其他可用车位后再排期。"))
    if inspector.on_leave:
        out.append(Conflict(
            code="inspector_leave", kind="resource", severity="blocking",
            title="查验员请假中",
            reason=f"查验员「{inspector.display_name}」已请假，请假期间不能派查验任务。"
                   f"请改派其他具备资质的查验员。"))
    if inspector.role != Role.INSPECTOR:
        out.append(Conflict(
            code="not_inspector", kind="resource", severity="blocking",
            title="指派对象不是查验员",
            reason=f"「{inspector.display_name}」角色不是查验员，不能承担现场查验。"))

    # —— 资质匹配：货 / 车位 / 人 三方 ——
    need_kind = QUAL_TO_BAY_KIND.get(need_qual)
    if need_qual and bay.kind != need_kind:
        out.append(Conflict(
            code="bay_qual_mismatch", kind="qual", severity="blocking",
            title="车位资质不匹配",
            reason=f"该货（{decl.cargo_name}，HS {decl.hs_code}）需「{QUAL_LABELS[need_qual]}」资质，"
                   f"应排 {BAY_KIND_LABELS[need_kind]}；当前选的「{bay.code}」是{BAY_KIND_LABELS.get(bay.kind, bay.kind)}，不具备对应查验条件。"))
    if not need_qual and bay.kind != "general":
        # 普货占专用台位：浪费专用资源，给 warning 不硬拦
        out.append(Conflict(
            code="bay_kind_waste", kind="qual", severity="warning",
            title="普货占用专用台位",
            reason=f"该票为普通货物，无需{BAY_KIND_LABELS.get(bay.kind, bay.kind)}；占用专用台位会挤掉特货产能，建议改排普货台位。",
        ))
    if need_qual and need_qual not in inspector.qual_list:
        out.append(Conflict(
            code="inspector_qual_mismatch", kind="qual", severity="blocking",
            title="查验员资质不匹配",
            reason=f"该货需「{QUAL_LABELS[need_qual]}」资质，查验员「{inspector.display_name}」"
                   f"现有资质为：{('、'.join(QUAL_LABELS.get(q, q) for q in inspector.qual_list)) or '仅普货'}，不能承接本票查验。"))

    # —— 时间 ——
    if start < datetime.utcnow() - timedelta(minutes=5):
        out.append(Conflict(
            code="scheduled_in_past", kind="time", severity="blocking",
            title="不能排在过去时间",
            reason=f"计划开始 {start:%Y-%m-%d %H:%M} 早于当前时间，查验排期只能面向当前及以后。"))

    if not in_business_window(yard, start):
        why = "周末场站不作业" if start.weekday() >= 5 and not yard.work_weekend else "不在每日营业时段"
        out.append(Conflict(
            code="out_of_window", kind="window", severity="warning",
            title="超出场站营业窗口",
            reason=f"{yard.name} 营业窗口为每{'天' if yard.work_weekend else '个工作日'} "
                   f"{float(yard.open_hour):g}:00–{float(yard.close_hour):g}:00。"
                   f"拟落位 {start:%Y-%m-%d %H:%M}（{why}）。确需非常规时段查验，须二次确认并填写原因，系统留痕。"))
    else:
        day_end = start.replace(hour=int(float(yard.close_hour)),
                                minute=int(round((float(yard.close_hour) % 1) * 60)),
                                second=0, microsecond=0)
        if finish > day_end:
            out.append(Conflict(
                code="cross_window", kind="window", severity="warning",
                title="查验时长超出闭场时间",
                reason=f"按 {minutes} 分钟计，查验将于 {finish:%H:%M} 结束，晚于场站闭场 "
                       f"{float(yard.close_hour):g}:00。须二次确认并填写原因。"))

    # —— 资源占用重叠：同车位同段两单 / 同查验员同时两单 ——
    # 车位冲突按车位查（车位隶属场站，天然隔离）；查验员冲突全局查（人可被多个场站调度）
    bay_others = db.query(Inspection).filter(
        Inspection.bay_id == bay.id,
        Inspection.status.in_(ACTIVE_TASK_STATUSES),
    ).all()
    for o in bay_others:
        if o.id == ignore_id:
            continue
        # 已放行/已结关/已撤销单的在档排期同样占资源且不可动（锁），重叠必报
        if overlaps(start, finish, o.scheduled_at, o.end_time):
            locked = o.declaration.status in LOCKED_DECL_STATUSES
            out.append(Conflict(
                code="bay_double_book", kind="bay", severity="blocking",
                title="同车位同时段两单占用",
                reason=(f"车位「{bay.code}」在 {start:%m-%d %H:%M}–{finish:%H:%M} 已排 "
                        f"{o.declaration.decl_no}（{o.scheduled_at:%H:%M}–{o.end_time:%H:%M}，{o.declaration.cargo_name}）"
                        + ("；该单已放行/结关，时间锁定不可移动。" if locked else "；同一车位同一时段不能查验两票货。")),
                conflict_with=_brief(o)))

    if inspector.id is not None:
        insp_others = db.query(Inspection).filter(
            Inspection.inspector_id == inspector.id,
            Inspection.status.in_(ACTIVE_TASK_STATUSES),
        ).all()
        for o in insp_others:
            if o.id == ignore_id:
                continue
            if overlaps(start, finish, o.scheduled_at, o.end_time):
                locked = o.declaration.status in LOCKED_DECL_STATUSES
                out.append(Conflict(
                    code="inspector_double_book", kind="inspector", severity="blocking",
                    title="同查验员同时两单",
                    reason=(f"查验员「{inspector.display_name}」在 {start:%m-%d %H:%M}–{finish:%H:%M} 已被 "
                            f"{o.declaration.decl_no}（{o.scheduled_at:%H:%M}–{o.end_time:%H:%M}，"
                            f"{o.yard.name if o.yard else ''}）占用"
                            + ("；该单已放行/结关，时间锁定不可移动。" if locked else "；一名查验员同一时段只能查一票。")),
                    conflict_with=_brief(o)))
    return out


def blocking(conflicts: list[Conflict]) -> list[Conflict]:
    return [c for c in conflicts if c.severity == "blocking"]


# ---------- 改派链式重排 ----------

@dataclass
class Shift:
    inspection_id: int
    decl_no: str
    cargo_name: str
    bay_code: str
    inspector_name: str
    old_start: datetime
    new_start: datetime
    new_end: datetime
    locked: bool
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "inspection_id": self.inspection_id,
            "decl_no": self.decl_no,
            "cargo_name": self.cargo_name,
            "bay_code": self.bay_code,
            "inspector_name": self.inspector_name,
            "old_start": self.old_start.isoformat(timespec="minutes"),
            "new_start": self.new_start.isoformat(timespec="minutes"),
            "new_end": self.new_end.isoformat(timespec="minutes"),
            "moved": self.old_start != self.new_start,
            "locked": self.locked,
            "reasons": self.reasons,
        }


def _resource_tasks(db: Session, yard_id: int, inspector_ids: set[int] | None = None) -> list[Inspection]:
    """参与资源占用计算的排期：本场站任务 + 相关查验员在其他场站的占用（不可移动屏障）。

    - 排除已完成/已取消（不占资源）；
    - 已放行/结关单即便未完成也作为锁定屏障；
    - 跨场站的同查验员任务只作为屏障出现，永不在本场站链条中移动。
    """
    rows = db.query(Inspection).filter(Inspection.yard_id == yard_id).all()
    keep = [r for r in rows if r.status in ACTIVE_TASK_STATUSES
            or (r.status == "done" and r.declaration.status in (DeclStatus.RELEASED, DeclStatus.CLOSED))]
    ids = set(inspector_ids or set())
    ids.update(r.inspector_id for r in keep if r.inspector_id)
    if ids:
        ext = db.query(Inspection).filter(
            Inspection.yard_id != yard_id,
            Inspection.inspector_id.in_(ids),
            Inspection.status.in_(ACTIVE_TASK_STATUSES),
        ).all()
        keep.extend(ext)
    return keep


def plan_chain_reassign(db: Session, *, target: Inspection, yard: Yard,
                        new_bay: Bay, new_inspector: User,
                        new_start: datetime, minutes: int) -> dict:
    """预演改派后的链式重排（不落库）。

    算法：目标单固定到新槽位 → 其余在场站资源线上的未锁定任务，按计划时间排序，
    逐条找「不早于原时间」的最近可行槽（同车位、同查验员两维都不与已固定任务重叠）。
    每条只后移、处理多轮直至无移动 → 时间严格单调，不可能成环。
    锁定单（已放行/结关/撤销 + 已完成查验）只当屏障不移动；撞上无处可挪则 blocked。
    """
    # 注意：目标单时间以用户选择为准（超窗口由 API 层要求二次确认填原因留痕，不静默挪动）；
    # 仅链条上被动顺延的任务才夹回营业窗口。
    resource_rows = _resource_tasks(db, yard.id, {new_inspector.id})
    external_ids = {t.id for t in resource_rows if t.yard_id != yard.id}
    tasks = [t for t in resource_rows if t.id != target.id]
    locked_ids = external_ids | {
        t.id for t in tasks
        if t.declaration.status in LOCKED_DECL_STATUSES or t.status == "done"
    }

    # 拟变更后的资源画像（目标单）
    proposed: dict[int, dict] = {
        target.id: {
            "start": new_start, "end": end_at(new_start, minutes),
            "bay_id": new_bay.id, "inspector_id": new_inspector.id,
            "locked": False, "inspection": target,
        }
    }
    for t in tasks:
        proposed[t.id] = {
            "start": t.scheduled_at, "end": t.end_time,
            "bay_id": t.bay_id, "inspector_id": t.inspector_id,
            "locked": t.id in locked_ids, "inspection": t,
        }

    shift_reasons: dict[int, list[str]] = {}

    def overlaps_any(p, fixed):
        """p 与已固定占用（目标单/锁定屏障/已就位前序单）在车位或查验员任一维重叠则返回冲突方。"""
        for q in fixed:
            if q is p:
                continue
            same_bay = p["bay_id"] is not None and p["bay_id"] == q["bay_id"]
            same_insp = p["inspector_id"] is not None and p["inspector_id"] == q["inspector_id"]
            if (same_bay or same_insp) and overlaps(p["start"], p["end"], q["start"], q["end"]):
                return ("bay" if same_bay else "inspector"), q
        return None

    # 车厢式链式顺延：按「原计划时间」排序单遍处理。每条只避让三类已固定占用——
    # ① 改派后的目标单 ② 锁定屏障（已放行/结关/已完成/跨场站）③ 已就位的更早任务。
    # 后处理的更晚任务只会被前面的挤后，绝不可能反过来把前面的再推走
    # → 传播方向严格沿时间向后，数学上不可能成环，也不会越过锁定单。
    movable = sorted((t for t in tasks if t.id not in locked_ids),
                     key=lambda t: (t.scheduled_at, t.id))
    fixed = [proposed[target.id]] + [proposed[t.id] for t in tasks if t.id in locked_ids]

    for t in movable:
        p = proposed[t.id]
        cand = t.scheduled_at  # 起点锚定原时间：只后移不前移
        p["start"] = cand
        p["end"] = end_at(cand, t.duration_minutes or 120)
        steps = 0
        last_hit = None
        while True:
            hit = overlaps_any(p, fixed)
            if not hit:
                break
            kind, q = hit
            other = q["inspection"]
            # 跳到冲突占用结束之后，再夹回营业窗口
            cand = clamp_to_window(yard, q["end"], t.duration_minutes or 120)
            p["start"] = cand
            p["end"] = end_at(cand, t.duration_minutes or 120)
            last_hit = (kind, q)
            steps += 1
            if steps > 200 or (cand - new_start).days > MAX_CHAIN_DAYS:
                break
            rsn = (f"为避开 {other.declaration.decl_no}（{'车位' if kind == 'bay' else '查验员'}"
                   f"占用至 {q['end']:%m-%d %H:%M}）顺延")
            if rsn not in shift_reasons.setdefault(t.id, []):
                shift_reasons[t.id].append(rsn)

        if (p["start"] - new_start).days > MAX_CHAIN_DAYS and last_hit and last_hit[1]["locked"]:
            kind, q = last_hit
            oi = q["inspection"]
            raise BizError(
                f"链式顺延被锁定单挡住：{oi.declaration.decl_no} 已放行/结关或完成，"
                f"其{'车位' if kind == 'bay' else '查验员'}占用不可移动；后续单顺延超过 {MAX_CHAIN_DAYS} 天仍无法避开。"
                f"请改用其他车位/查验员，或由监管员手工协调。",
                code="chain_blocked_by_locked")
        # 本单就位，加入固定集合供后续更晚任务避让
        fixed.append(p)

    # 组装结果：目标单 + 实际移动的链条（未移动不回传，减少噪音）
    shifts = []
    for t in [target] + movable:
        p = proposed[t.id]
        moved = p["start"] != t.scheduled_at or t.id == target.id
        if t.id == target or p["start"] != t.scheduled_at:
            shifts.append(Shift(
                inspection_id=t.id,
                decl_no=t.declaration.decl_no,
                cargo_name=t.declaration.cargo_name,
                bay_code=t.bay.code if t.bay else "",
                inspector_name=t.inspector.display_name if t.inspector else "",
                old_start=t.scheduled_at,
                new_start=p["start"],
                new_end=p["end"],
                locked=False,
                reasons=shift_reasons.get(t.id, ["改派目标单，按新资源重排"] if t.id == target.id else []),
            ))
    return {
        "proposed": proposed,
        "shifts": shifts,
        "locked_barriers": [_brief(t) for t in tasks if t.id in locked_ids],
        "new_start": new_start,
    }


def log_change(db: Session, *, inspection: Inspection | None, actor: User, ctype: str,
               reason: str, detail: dict, chain_batch: str | None = None,
               declaration_id: int | None = None, yard_id: int | None = None) -> ScheduleChange:
    ch = ScheduleChange(
        inspection_id=inspection.id if inspection else None,
        declaration_id=declaration_id or (inspection.declaration_id if inspection else None),
        yard_id=yard_id or (inspection.yard_id if inspection else None),
        actor_id=actor.id,
        actor_name=actor.display_name,
        change_type=ctype,
        reason=reason or "",
        detail=json.dumps(detail, ensure_ascii=False, default=str),
        chain_batch=chain_batch,
    )
    db.add(ch)
    return ch
