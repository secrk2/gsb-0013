"""报关作战台：按企业聚合的待审漏斗 + 查验排期 + 征税/逾期红点。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    User, Role, Enterprise, Declaration, Inspection, DeclStatus, STATUS_LABELS,
)
from ..deps import get_current_user
from ..schemas import inspection_out

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

FUNNEL_STAGES = [
    (DeclStatus.ENTRUSTED, "委托中"),
    (DeclStatus.ENTERED, "已录入"),
    (DeclStatus.REVIEWING, "审单中"),
    (DeclStatus.INSPECTING, "查验中"),
    (DeclStatus.RELEASED, "已放行"),
    (DeclStatus.CLOSED, "已结关"),
]

TAX_WARNING_DAYS = 3  # 距缴款期限 ≤3 天出现红点


@router.get("/overview")
def overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.utcnow()

    ent_q = db.query(Enterprise)
    if user.role == Role.ENTERPRISE:
        ent_q = ent_q.filter(Enterprise.id == user.enterprise_id)
    enterprises = ent_q.order_by(Enterprise.id).all()

    cards = []
    totals = {s.value: 0 for s, _ in FUNNEL_STAGES}
    overdue_tax_count = 0
    due_soon_count = 0
    unpaid_count = 0

    for ent in enterprises:
        decls = db.query(Declaration).filter(Declaration.enterprise_id == ent.id).all()
        funnel = []
        for st, label in FUNNEL_STAGES:
            n = sum(1 for d in decls if d.status == st)
            totals[st.value] += n
            funnel.append({"status": st.value, "label": label, "count": n})
        cancelled = sum(1 for d in decls if d.status == DeclStatus.CANCELLED)

        tax_items = []
        for d in decls:
            if float(d.tax_amount or 0) <= 0:
                continue
            flag = "paid"
            if not d.tax_paid and d.tax_due_date:
                days_left = (d.tax_due_date - now).total_seconds() / 86400
                if d.tax_due_date < now:
                    flag = "overdue"
                    overdue_tax_count += 1
                elif days_left <= TAX_WARNING_DAYS:
                    flag = "due_soon"
                    due_soon_count += 1
                else:
                    flag = "unpaid"
                unpaid_count += 1
            tax_items.append({
                "decl_id": d.id,
                "decl_no": d.decl_no,
                "tax_amount": float(d.tax_amount),
                "tax_paid": d.tax_paid,
                "tax_due_date": d.tax_due_date.isoformat(timespec="seconds") if d.tax_due_date else None,
                "days_left": None if (d.tax_paid or not d.tax_due_date) else round((d.tax_due_date - now).total_seconds() / 86400, 1),
                "flag": flag,
                "status": d.status.value,
                "status_label": STATUS_LABELS[d.status],
            })

        pending_review = sum(1 for d in decls if d.status in (DeclStatus.ENTERED, DeclStatus.REVIEWING))
        active_count = sum(1 for d in decls if d.status not in (DeclStatus.CLOSED, DeclStatus.CANCELLED))
        cards.append({
            "enterprise_id": ent.id,
            "enterprise_name": ent.name_short,
            "code": ent.code,
            "ent_status": ent.status.value,
            "active_declarations": active_count,
            "pending_review_count": pending_review,
            "tax_overdue_count": sum(1 for t in tax_items if t["flag"] == "overdue"),
            "tax_due_soon_count": sum(1 for t in tax_items if t["flag"] == "due_soon"),
            "funnel": funnel,
            "cancelled": cancelled,
            "tax_items": sorted(tax_items, key=lambda t: (t["tax_paid"], t["tax_due_date"] or "9999")),
        })

    # 查验排期（全局或本企业）
    insp_q = db.query(Inspection)
    if user.role == Role.ENTERPRISE:
        insp_q = insp_q.join(Declaration).filter(Declaration.enterprise_id == user.enterprise_id)
    inspections = insp_q.order_by(Inspection.scheduled_at).all()
    schedule = []
    for i in inspections:
        item = inspection_out(i)
        item["overdue"] = i.status in ("scheduled", "inspecting", "abnormal") and i.scheduled_at < now
        item["mine"] = (user.role == Role.BROKER and i.declaration.broker_id == user.id)
        schedule.append(item)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "totals_funnel": [{"status": s.value, "label": l, "count": totals[s.value]} for s, l in FUNNEL_STAGES],
        "enterprises": cards,
        "inspection_schedule": schedule,
        "red_dots": {
            "tax_overdue": overdue_tax_count,
            "tax_due_soon": due_soon_count,
            "tax_unpaid": unpaid_count,
            "inspection_today": sum(
                1 for i in inspections
                if i.status in ("scheduled", "inspecting") and i.scheduled_at.date() == now.date()
            ),
            "inspection_overdue": sum(
                1 for i in inspections
                if i.status in ("scheduled", "inspecting", "abnormal") and i.scheduled_at < now
            ),
        },
        "tax_warning_days": TAX_WARNING_DAYS,
    }
