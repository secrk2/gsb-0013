"""查验排期领域服务：资质推断、冲突检测、时段建议、链式重排。

冲突分两级：
- hard（硬冲突）：同车位同时段两单占、同查验员同时段两单、资质不匹配、
  资源停用/请假 —— 一律拦下，结构化逐条返回，前端逐条标红、点开看原因；
- soft（窗口提醒）：落在场站每日作业时段外、超出排期天数窗口、排到过去时间 ——
  不绝对禁止，但必须二次确认并填写原因留痕（window_override）。

链式重排「不出环形依赖」的保证：只做一次「按计划时间升序的单向扫描」，
每单只会向后顺延（时间不减）、不回看引用后续单的新位置，因此结构上不可能成环；
已放行/已结关/已完成/已取消单只作为占用约束，本身绝不移动。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Inspection, DeclStatus

# ---------- 资质字典 ----------

CERT_LABELS = {
    "normal": "普通货物",
    "cold": "冷链温控",
    "food": "食品检疫",
    "heavy": "大型机械/重吊",
    "danger": "危化品监管",
}

# HS 章（前 2 位）→ 需食品检疫资质
_FOOD_CHAPTERS = {"02", "03", "04", "07", "08", "09", "15", "16", "17", "18", "19", "20", "21", "22"}
# HS 章 → 危化品监管
_DANGER_CHAPTERS = {"28", "29", "36", "38"}
_COLD_KEYWORDS = ("冻", "冷", "冷链", "冷藏", "冰鲜", "温控")
_FOOD_KEYWORDS = ("食品", "三文鱼", "龙虾", "牛腩", "肉", "菜", "水产", "海鲜", "水产", "礼盒食品")
_HEAVY_KEYWORDS = ("机床", "注塑机", "重型", "工程机械", "整机", "锅炉", "起重", "叉车", "轴承组件", "数控")
_DANGER_KEYWORDS = ("危化", "易燃", "腐蚀", "剧毒", "化工品", "油漆", "电池液")


def split_certs(text: str | None) -> set[str]:
    return {t.strip() for t in (text or "").split(",") if t.strip()}


def infer_required_certs(hs_code: str, cargo_name: str) -> list[str]:
    """按 HS 章 + 货名关键词推断该单需要的查验资质。"""
    chapter = (hs_code or "")[:2]
    name = cargo_name or ""
    certs: set[str] = set()
    if chapter in _FOOD_CHAPTERS or any(k in name for k in _FOOD_KEYWORDS):
        certs.add("food")
    if any(k in name for k in _COLD_KEYWORDS):
        certs.add("cold")
    if chapter in _DANGER_CHAPTERS or any(k in name for k in _DANGER_KEYWORDS):
        certs.add("danger")
    if any(k in name for k in _HEAVY_KEYWORDS):
        certs.add("heavy")
    if not certs:
        certs.add("normal")
    # 固定输出顺序，避免界面/留痕里顺序抖动
    order = ["normal", "cold", "food", "heavy", "danger"]
    return [c for c in order if c in certs]


def cert_labels(certs) -> list[str]:
    return [CERT_LABELS.get(c, c) for c in certs]


# ---------- 冲突检测 ----------

ACTIVE_STATUSES = ("scheduled", "inspecting")  # 占位中的排期（取消/完成不再占资源）


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    """半开区间 [start, end) 重叠判定：首尾相接不算撞。"""
    return start < other_end and end > other_start


def _active_query(db: Session):
    return db.query(Inspection).filter(Inspection.status.in_(ACTIVE_STATUSES))


def find_hard_conflicts(db: Session, *, yard_id: int, bay, inspector,
                        start: datetime, end: datetime,
                        required_certs: list[str], exclude_inspection_id: int | None = None) -> list[dict]:
    """返回硬冲突明细列表（空列表=无冲突）。每条都可直接给前端标红+点开看原因。"""
    conflicts: list[dict] = []
    required = set(required_certs)

    # 1) 车位停用（车故障）
    if bay is not None and bay.out_of_service:
        conflicts.append({
            "code": "bay_out_of_service",
            "severity": "hard",
            "title": "车位故障停用",
            "reason": f"车位「{bay.name or bay.code}」当前故障停用（{bay.out_of_service_reason or '原因未登记'}），"
                      f"不能安排查验，请改派其他具备{ '、'.join(cert_labels(required)) }资质的车位。",
        })

    # 2) 车位资质不匹配
    if bay is not None:
        bay_certs = split_certs(bay.cert_tags)
        missing = required - bay_certs
        if missing:
            conflicts.append({
                "code": "bay_cert_mismatch",
                "severity": "hard",
                "title": "车位资质不匹配",
                "reason": f"该单需要「{'、'.join(cert_labels(sorted(missing)))}」查验条件，"
                          f"车位「{bay.name or bay.code}」仅具备「{'、'.join(cert_labels(sorted(bay_certs))) or '无'}」资质，"
                          f"不能在此车位查验（冷链/食品/危化/重机须专区）。",
            })

    # 3) 同车位同段两单占
    if bay is not None:
        q = _active_query(db).filter(
            Inspection.yard_id == yard_id, Inspection.bay_id == bay.id,
            Inspection.scheduled_at < end, Inspection.scheduled_end > start,
        )
        if exclude_inspection_id:
            q = q.filter(Inspection.id != exclude_inspection_id)
        for other in q.all():
            conflicts.append({
                "code": "bay_overlap",
                "severity": "hard",
                "title": "同车位同时段两单占用",
                "reason": f"车位「{bay.name or bay.code}」在 {fmt(other.scheduled_at)}–{fmt(other.scheduled_end)} "
                          f"已排 {other.declaration.decl_no}（{other.declaration.cargo_name}），"
                          f"与本单时段重叠，同一车位不能同时查验两单。",
                "related": _related(other),
            })

    # 4) 查验员停用（请假）
    if inspector is not None and not inspector.available:
        conflicts.append({
            "code": "inspector_unavailable",
            "severity": "hard",
            "title": "查验员请假/不可派工",
            "reason": f"查验员 {inspector.display_name} 当前不可派工"
                      f"（{inspector.unavailable_reason or '已停用'}），请改派其他查验员。",
        })

    # 5) 查验员资质不匹配
    if inspector is not None:
        insp_certs = split_certs(inspector.inspector_certs)
        missing = required - insp_certs
        if missing:
            conflicts.append({
                "code": "inspector_cert_mismatch",
                "severity": "hard",
                "title": "查验员资质不匹配",
                "reason": f"该单需要查验员具备「{'、'.join(cert_labels(sorted(missing)))}」资质，"
                          f"{inspector.display_name} 的资质为「{'、'.join(cert_labels(sorted(insp_certs))) or '无'}」，"
                          f"资质不符不能派工。",
            })

    # 6) 同查验员同时两单
    if inspector is not None:
        q = _active_query(db).filter(
            Inspection.inspector_id == inspector.id,
            Inspection.scheduled_at < end, Inspection.scheduled_end > start,
        )
        if exclude_inspection_id:
            q = q.filter(Inspection.id != exclude_inspection_id)
        for other in q.all():
            conflicts.append({
                "code": "inspector_overlap",
                "severity": "hard",
                "title": "同查验员同时段两单",
                "reason": f"查验员 {inspector.display_name} 在 {fmt(other.scheduled_at)}–{fmt(other.scheduled_end)} "
                          f"已被 {other.declaration.decl_no}（{other.declaration.port} {other.bay_ref.name if other.bay_ref else other.bay}）占用，"
                          f"同一时段不能查验两单。",
                "related": _related(other),
            })

    return conflicts


def _related(insp: Inspection) -> dict:
    return {
        "inspection_id": insp.id,
        "declaration_id": insp.declaration_id,
        "decl_no": insp.declaration.decl_no,
        "cargo_name": insp.declaration.cargo_name,
        "scheduled_at": insp.scheduled_at.isoformat(timespec="minutes"),
        "scheduled_end": insp.scheduled_end.isoformat(timespec="minutes"),
    }


def find_window_warnings(yard, start: datetime, end: datetime, now: datetime) -> list[dict]:
    """软提醒：超出场站排期窗口（作业时段/可排天数/过去时间），需二次确认填原因。"""
    warnings: list[dict] = []
    if start < now - timedelta(minutes=5):
        warnings.append({
            "code": "scheduled_in_past",
            "severity": "soft",
            "title": "排期时间已过",
            "reason": f"计划开始时间 {fmt(start)} 早于当前时间，属于补录/历史排期，需要填写原因确认。",
        })
    if start.hour < yard.open_hour or end.date() > start.date() or end.hour > yard.close_hour or \
            (end.hour == yard.close_hour and (end.minute or end.second)):
        warnings.append({
            "code": "outside_working_hours",
            "severity": "soft",
            "title": "超出每日作业时段",
            "reason": f"落点 {fmt(start)}–{fmt(end)} 在场站每日作业时段 "
                      f"{yard.open_hour:02d}:00–{yard.close_hour:02d}:00 之外（含跨午夜），"
                      f"夜间/清晨查验需二次确认并写明原因。",
        })
    horizon_end = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                   + timedelta(days=yard.horizon_days + 1))
    if start > horizon_end:
        warnings.append({
            "code": "beyond_horizon",
            "severity": "soft",
            "title": f"超出 {yard.horizon_days} 天排期窗口",
            "reason": f"落点 {fmt(start)} 超出该场站未来 {yard.horizon_days} 天的排期窗口"
                      f"（可排至 {horizon_end.date()}），远期排期需二次确认并写明原因。",
        })
    return warnings


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------- 时段建议 ----------

def _grid_candidates(start: datetime, end_limit: datetime, step_min: int = 30):
    """从 start 向上吸附到整/半点，在作业时段内逐个产出候选开始时间（跳过非作业时段）。"""
    cur = start
    # 吸附到 30 分钟网格
    minute = cur.minute
    if minute % step_min:
        cur += timedelta(minutes=step_min - minute % step_min)
    cur = cur.replace(second=0, microsecond=0)
    while cur < end_limit:
        yield cur
        cur += timedelta(minutes=step_min)


def suggest_slots(db: Session, *, yard, required_certs: list[str],
                  duration_minutes: int = 120, start_from: datetime | None = None,
                  limit: int = 6) -> list[dict]:
    """推荐「资质匹配车位 + 资质匹配查验员 + 双方均空闲」的最早若干时段。"""
    now = datetime.utcnow()
    start = start_from or now + timedelta(hours=1)
    end_limit = now + timedelta(days=min(yard.horizon_days, 14))
    required = set(required_certs)

    ok_bays = [b for b in yard.bays
               if not b.out_of_service and required <= split_certs(b.cert_tags)]
    ok_inspectors = [u for u in yard.inspectors
                     if u.available and required <= split_certs(u.inspector_certs)]
    if not ok_bays or not ok_inspectors:
        return []

    duration = timedelta(minutes=duration_minutes)
    suggestions = []
    for cand in _grid_candidates(start, end_limit):
        if cand.hour < yard.open_hour:
            continue
        cand_end = cand + duration
        # 跨日/超出作业时段的候选直接跳过（建议只给正常窗口内的）
        if cand_end.date() > cand.date() or cand_end.hour > yard.close_hour:
            continue
        for bay in ok_bays:
            bay_busy = _active_query(db).filter(
                Inspection.yard_id == yard.id, Inspection.bay_id == bay.id,
                Inspection.scheduled_at < cand_end, Inspection.scheduled_end > cand,
            ).first()
            if bay_busy:
                continue
            for insp in ok_inspectors:
                insp_busy = _active_query(db).filter(
                    Inspection.inspector_id == insp.id,
                    Inspection.scheduled_at < cand_end, Inspection.scheduled_end > cand,
                ).first()
                if insp_busy:
                    continue
                suggestions.append({
                    "scheduled_at": cand.isoformat(timespec="minutes"),
                    "scheduled_end": cand_end.isoformat(timespec="minutes"),
                    "bay_id": bay.id, "bay_name": bay.name or bay.code,
                    "inspector_id": insp.id, "inspector_name": insp.display_name,
                })
                break  # 每个时段一个推荐即可
            if len(suggestions) >= limit:
                return suggestions
            break  # 该时段已有车位方案，尝试下一时段
    return suggestions


# ---------- 链式重排 ----------

def _feasible(db, yard, *, start, duration, required, keep_bay, keep_inspector,
              forbidden_bay_ids: set[int], forbidden_inspector_ids: set[int],
              occupancy_bay: dict, occupancy_insp: dict):
    """在 start 起找最早可行（车位+查验员）组合，四阶段偏好：

    1. 同车位 + 同查验员，仅向后顺延（链式重排的自然语义）；
    2. 同查验员，换合规车位；
    3. 同车位，换合规查验员；
    4. 两者都可换（资源故障/请假时才会走到）。
    返回 (start, bay, inspector)；14 天窗口内找不到返回 None。
    """
    end_limit = start + timedelta(days=14)
    all_bays = [b for b in yard.bays
                if not b.out_of_service and b.id not in forbidden_bay_ids
                and required <= split_certs(b.cert_tags)]
    all_insp = [u for u in yard.inspectors
                if u.available and u.id not in forbidden_inspector_ids
                and required <= split_certs(u.inspector_certs)]
    if not all_bays or not all_insp:
        return None

    def ordered(keep, pool):
        """原资源排最前，其余按 id 稳定排序。"""
        head = [keep] if keep is not None and keep in pool else []
        return head + [x for x in pool if keep is None or x.id != keep.id]

    def search(bays, inspectors):
        for cand in _grid_candidates(start, end_limit):
            if cand.hour < yard.open_hour:
                continue
            cand_end = cand + duration
            if cand_end.date() > cand.date() or cand_end.hour > yard.close_hour:
                continue
            bay = next((b for b in bays
                        if not _occ(occupancy_bay.get(b.id), cand, cand_end)), None)
            if not bay:
                continue
            insp = next((u for u in inspectors
                         if not _occ(occupancy_insp.get(u.id), cand, cand_end)), None)
            if not insp:
                continue
            return cand, bay, insp
        return None

    bays_keep = ordered(keep_bay, all_bays)
    insp_keep = ordered(keep_inspector, all_insp)
    # 阶段 1：车位/查验员都锁原资源
    if keep_bay in all_bays and keep_inspector in all_insp:
        found = search([keep_bay], [keep_inspector])
        if found:
            return found
    # 阶段 2：锁定原查验员，车位可换（原车位优先）
    if keep_inspector in all_insp:
        found = search(bays_keep, [keep_inspector])
        if found:
            return found
    # 阶段 3：锁定原车位，查验员可换（原查验员优先）
    if keep_bay in all_bays:
        found = search([keep_bay], insp_keep)
        if found:
            return found
    # 阶段 4：资源均可换
    return search(bays_keep, insp_keep)


def _occ(intervals, start, end) -> bool:
    if not intervals:
        return False
    return any(s < end and e > start for s, e in intervals)


def plan_reassign_chain(db: Session, *, anchor: Inspection, new_start: datetime,
                        new_bay, new_inspector, duration_minutes: int,
                        forbidden_bay_ids: set[int] | None = None,
                        forbidden_inspector_ids: set[int] | None = None) -> dict:
    """预览/执行共用的链式重排推演（纯计算，不写库）。

    返回 anchor / moves / blocked / skipped_terminal / conflicts。
    """
    forbidden_bay_ids = forbidden_bay_ids or set()
    forbidden_inspector_ids = forbidden_inspector_ids or set()
    yard = anchor.yard
    now = datetime.utcnow()
    duration = timedelta(minutes=duration_minutes)
    required = split_certs(anchor.required_certs)

    # —— 收集同场站全部排期，分类：固定约束 / 可顺延 / 跳过（终态）——
    all_in_yard = db.query(Inspection).filter(Inspection.yard_id == yard.id).all()
    fixed: list[Inspection] = []
    movable: list[Inspection] = []
    skipped_terminal: list[dict] = []
    seen_terminal_decls: set[int] = set()
    # 终态单只有落在「锚点前 1 天 ~ 锚点后 14 天」窗口内才需要在预览里提示，
    # 几个月前的历史结关单既不可能与新排期重叠，也不该刷屏；占用约束仍然全量保留。
    report_lo = anchor.scheduled_at - timedelta(days=1)
    report_hi = anchor.scheduled_at + timedelta(days=14)

    def report_terminal(insp: Inspection, label: str, reason: str):
        if insp.declaration_id in seen_terminal_decls:
            return
        if not (report_lo <= insp.scheduled_at <= report_hi):
            return
        seen_terminal_decls.add(insp.declaration_id)
        skipped_terminal.append({
            "inspection_id": insp.id, "decl_no": insp.declaration.decl_no,
            "status": insp.declaration.status.value,
            "status_label": label, "reason": reason,
        })

    for insp in all_in_yard:
        if insp.id == anchor.id:
            continue
        decl_status = insp.declaration.status
        # 已放行/已结关：绝对不动，仅记录「为何跳过」（同一单有多条查验时只报一次）
        if decl_status in (DeclStatus.RELEASED, DeclStatus.CLOSED):
            report_terminal(
                insp,
                "已放行" if decl_status == DeclStatus.RELEASED else "已结关",
                "报关单已放行/已结关，链式重排不得改动其查验记录")
            fixed.append(insp)
            continue
        if insp.status in ("done", "abnormal"):
            # 已完成作为历史占用约束；异常挂起单未结案，车位/人员仍视为被占用
            fixed.append(insp)
            continue
        if insp.status == "inspecting":
            # 正在查验：不可挪动，作为硬占用
            fixed.append(insp)
            continue
        if insp.status == "cancelled":
            # 已取消不占资源、不移动
            continue
        # scheduled：按 (计划时间, id) 名次定义「后续单」。
        # 名次在锚点之前的不动（固定约束），锚点及其之后的进入链式顺延；
        # 只往后推、不回看 → 结构上不可能形成环形依赖。
        rank = (insp.scheduled_at, insp.id)
        anchor_rank = (anchor.scheduled_at, anchor.id)
        if rank < anchor_rank:
            fixed.append(insp)
        else:
            movable.append(insp)

    movable.sort(key=lambda x: (x.scheduled_at, x.id))

    occupancy_bay: dict[int, list] = {}
    occupancy_insp: dict[int, list] = {}

    def occupy(bay_id, insp_id, s, e):
        if bay_id is not None:
            occupancy_bay.setdefault(bay_id, []).append((s, e))
        if insp_id is not None:
            occupancy_insp.setdefault(insp_id, []).append((s, e))

    for f in fixed:
        occupy(f.bay_id, f.inspector_id, f.scheduled_at, f.scheduled_end)

    # —— 1) 锚点单：放在用户指定的新资源/新时间 ——
    anchor_end = new_start + duration
    anchor_conflicts = find_hard_conflicts(
        db, yard_id=yard.id, bay=new_bay, inspector=new_inspector,
        start=new_start, end=anchor_end, required_certs=list(required),
        exclude_inspection_id=anchor.id,
    )
    # 与「后续可顺延单」的重叠不算硬冲突（它们会被链推走）；只保留与固定约束的冲突
    movable_ids = {m.id for m in movable}
    real_conflicts = []
    for c in anchor_conflicts:
        rel = c.get("related")
        if rel and rel["inspection_id"] in movable_ids:
            continue
        real_conflicts.append(c)
    if real_conflicts:
        return {"ok": False, "conflicts": real_conflicts, "moves": [], "blocked": [],
                "skipped_terminal": skipped_terminal}

    moves: list[dict] = []
    blocked: list[dict] = []
    # 记录锚点新占用（旧占用已随解绑释放，本就不在 occupancy 里）
    occupy(new_bay.id if new_bay else None, new_inspector.id if new_inspector else None,
           new_start, anchor_end)
    moves.append({
        "inspection_id": anchor.id,
        "declaration_id": anchor.declaration_id,
        "decl_no": anchor.declaration.decl_no,
        "cargo_name": anchor.declaration.cargo_name,
        "is_anchor": True,
        "from_scheduled_at": anchor.scheduled_at.isoformat(timespec="minutes"),
        "to_scheduled_at": new_start.isoformat(timespec="minutes"),
        "to_scheduled_end": anchor_end.isoformat(timespec="minutes"),
        "from_bay_id": anchor.bay_id,
        "from_bay_name": anchor.bay_ref.name if anchor.bay_ref else anchor.bay,
        "to_bay_id": new_bay.id if new_bay else None,
        "to_bay_name": (new_bay.name or new_bay.code) if new_bay else None,
        "from_inspector_id": anchor.inspector_id,
        "from_inspector_name": anchor.inspector.display_name if anchor.inspector else None,
        "to_inspector_id": new_inspector.id if new_inspector else None,
        "to_inspector_name": new_inspector.display_name if new_inspector else None,
    })

    # —— 2) 后续单：按时间升序单向扫描，最小移动原则 ——
    for m in movable:
        req = split_certs(m.required_certs) or {"normal"}
        dur = m.scheduled_end - m.scheduled_at
        cur_bay, cur_insp = m.bay_ref, m.inspector

        # 2a) 原车位+原查验员+原时间仍可行则不动（超窗口排期是已二次确认留痕的合法排期，
        #     链不应无故挪动它；只在被链上变更挤出时才顺延）
        current_ok = (
            cur_bay is not None and cur_insp is not None
            and cur_bay.id not in forbidden_bay_ids and cur_insp.id not in forbidden_inspector_ids
            and not cur_bay.out_of_service and cur_insp.available
            and req <= split_certs(cur_bay.cert_tags)
            and req <= split_certs(cur_insp.inspector_certs)
            and not _occ(occupancy_bay.get(cur_bay.id), m.scheduled_at, m.scheduled_end)
            and not _occ(occupancy_insp.get(cur_insp.id), m.scheduled_at, m.scheduled_end)
        )
        if current_ok:
            occupy(cur_bay.id, cur_insp.id, m.scheduled_at, m.scheduled_end)
            continue

        # 2b) 原位置被挤出 → 从原时间起向后找最早可行组合（只后移、不回看 → 不成环）
        found = _feasible(
            db, yard, start=m.scheduled_at, duration=dur,
            required=req,
            keep_bay=cur_bay, keep_inspector=cur_insp,
            forbidden_bay_ids=forbidden_bay_ids, forbidden_inspector_ids=forbidden_inspector_ids,
            occupancy_bay=occupancy_bay, occupancy_insp=occupancy_insp,
        )
        if not found:
            blocked.append({
                "inspection_id": m.id, "decl_no": m.declaration.decl_no,
                "cargo_name": m.declaration.cargo_name,
                "scheduled_at": m.scheduled_at.isoformat(timespec="minutes"),
                "reason": "14 天排期窗口内找不到同时满足资质与空闲的车位/查验员组合，需人工另定场站或扩窗",
            })
            continue
        s, bay, insp = found
        e = s + dur
        occupy(bay.id, insp.id, s, e)
        moves.append({
            "inspection_id": m.id,
            "declaration_id": m.declaration_id,
            "decl_no": m.declaration.decl_no,
            "cargo_name": m.declaration.cargo_name,
            "is_anchor": False,
            "from_scheduled_at": m.scheduled_at.isoformat(timespec="minutes"),
            "to_scheduled_at": s.isoformat(timespec="minutes"),
            "to_scheduled_end": e.isoformat(timespec="minutes"),
            "from_bay_id": m.bay_id,
            "from_bay_name": m.bay_ref.name if m.bay_ref else m.bay,
            "to_bay_id": bay.id,
            "to_bay_name": bay.name or bay.code,
            "from_inspector_id": m.inspector_id,
            "from_inspector_name": m.inspector.display_name if m.inspector else None,
            "to_inspector_id": insp.id,
            "to_inspector_name": insp.display_name,
        })

    return {"ok": True, "conflicts": [], "moves": moves, "blocked": blocked,
            "skipped_terminal": skipped_terminal}


def new_batch_id() -> str:
    return "RA-" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + "-" + f"{random.randint(0, 0xffff):04x}"
