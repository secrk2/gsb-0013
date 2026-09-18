"""客户与委托：企业备案（脱敏展示）、二次确认看全名留痕、委托合同签署。

委托链路顺序强校验：备案中 → 不得签合同；合同未生效 → 不得立项报关单。
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    User, Role, Enterprise, Contract, EnterpriseStatus, ContractStatus,
    NameReveal, BizError,
)
from ..schemas import (
    EnterpriseFileIn, ContractSignIn, RevealNameIn,
    enterprise_out, contract_out, user_out,
)
from ..deps import get_current_user, require_roles

router = APIRouter(tags=["clients"])


# 行政区划前缀：先匹配「地名+行政后缀」（深圳市），再回退裸城市名（宁波、广州…）
_REGION_PREFIX = re.compile(
    r"^([一-龥]{1,8}?(省|市|区|县|自治区|特别行政区)|"
    r"北京|上海|天津|重庆|深圳|广州|宁波|厦门|杭州|南京|苏州|青岛|大连|成都|武汉|西安|"
    r"长沙|郑州|合肥|福州|济南|沈阳|哈尔滨|长春|昆明|南昌|贵阳|南宁|海口|三亚|兰州|"
    r"乌鲁木齐|拉萨|银川|西宁|呼和浩特|石家庄|太原|佛山|东莞|珠海|汕头|湛江|钦州|防城|"
    r"烟台|威海|日照|连云港|南通|镇江|温州|嘉兴|金华|泉州|漳州|九江|宜昌|岳阳|芜湖)"
)
# 公司组织形式后缀
_LEGAL_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团", "公司")
# 行业/经营特征词（从字号右侧剥离，长的优先）
_INDUSTRY_WORDS = (
    "国际供应链", "供应链", "国际贸易", "进出口贸易", "进出口", "外贸",
    "精密机械", "电子科技", "冷链食品", "网络科技", "信息科技", "生物科技",
    "机械", "电器", "电子", "科技", "食品", "冷链", "物流", "贸易",
    "国际", "实业", "集团",
)


def _abbreviate(name: str) -> str:
    """企业名脱敏：剥地名前缀、行业词、组织形式后取字号核心（2~3 字）。

    例：「深圳市华腾国际供应链有限公司」→「华腾」；
        「宁波远航精密机械进出口有限公司」→「远航」。
    """
    core = name.strip()
    changed = True
    while changed:
        changed = False
        m = _REGION_PREFIX.match(core)
        if m:
            core = core[m.end():]
            changed = True
    for suffix in _LEGAL_SUFFIXES:
        if core.endswith(suffix):
            core = core[: -len(suffix)]
            break
    # 反复剥右侧行业词，直到露出字号
    moved = True
    while moved and len(core) > 2:
        moved = False
        for w in _INDUSTRY_WORDS:
            if core.endswith(w) and len(core) - len(w) >= 2:
                core = core[: -len(w)]
                moved = True
                break
    # 字号取前 2~3 字
    brand = core[:3] if len(core) > 3 else core
    return brand or name[:2]


def make_masked_name(full_name: str, code: str) -> str:
    brand = _abbreviate(full_name)
    digits = "".join(ch for ch in code if ch.isdigit())
    serial = digits[-4:].zfill(4) if digits else code[-4:]
    return f"{brand} E{serial}"


@router.get("/enterprises")
def list_enterprises(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Enterprise)
    if user.role == Role.ENTERPRISE:
        q = q.filter(Enterprise.id == user.enterprise_id)
    items = q.order_by(Enterprise.id).all()
    result = []
    for e in items:
        out = enterprise_out(e, reveal_full=False)
        # 该报关员若在当前会话已做过二次确认，则本次列表直接给出全名（仍以留痕为前提）
        out["contract_count"] = len(e.contracts)
        out["declaration_count"] = len(e.declarations)
        result.append(out)
    return result


@router.post("/enterprises/file")
def file_enterprise(body: EnterpriseFileIn,
                    user: User = Depends(require_roles(Role.BROKER, Role.SUPERVISOR)),
                    db: Session = Depends(get_db)):
    if db.query(Enterprise).filter(Enterprise.code == body.code).first():
        raise BizError(f"海关注册编码 {body.code} 已备案，不能重复备案", code="duplicate_code", status_code=400)
    if db.query(Enterprise).filter(Enterprise.name_full == body.name_full).first():
        raise BizError("该企业全称已存在备案记录，不能重复建档", code="duplicate_name", status_code=400)

    ent = Enterprise(
        code=body.code,
        name_full=body.name_full.strip(),
        name_short=make_masked_name(body.name_full.strip(), body.code),
        credit_code=body.credit_code,
        contact_person=body.contact_person,
        contact_phone=body.contact_phone,
        ie_flag=body.ie_flag,
        status=EnterpriseStatus.FILING,
    )
    db.add(ent)
    db.commit()
    db.refresh(ent)
    return {"ok": True, "enterprise": enterprise_out(ent),
            "hint": "企业已提交备案，状态为「备案中」；备案审核通过后才能签署委托合同。"}


@router.post("/enterprises/{ent_id}/approve")
def approve_enterprise(ent_id: int,
                       user: User = Depends(require_roles(Role.SUPERVISOR, Role.CUSTOMS)),
                       db: Session = Depends(get_db)):
    ent = db.get(Enterprise, ent_id)
    if not ent:
        raise BizError("企业不存在", code="not_found", status_code=404)
    if ent.status == EnterpriseStatus.ACTIVE:
        raise BizError(f"企业「{ent.name_short}」已备案通过，无需重复审核", code="already_active")
    ent.status = EnterpriseStatus.ACTIVE
    ent.filed_at = datetime.utcnow()
    ent.reject_reason = ""
    db.commit()
    return {"ok": True, "enterprise": enterprise_out(ent)}


@router.post("/enterprises/{ent_id}/reject")
def reject_enterprise(ent_id: int, reason: str = "备案资料不齐或核验不通过",
                      user: User = Depends(require_roles(Role.SUPERVISOR, Role.CUSTOMS)),
                      db: Session = Depends(get_db)):
    ent = db.get(Enterprise, ent_id)
    if not ent:
        raise BizError("企业不存在", code="not_found", status_code=404)
    ent.status = EnterpriseStatus.REJECTED
    ent.reject_reason = reason
    db.commit()
    return {"ok": True, "enterprise": enterprise_out(ent)}


@router.post("/enterprises/reveal")
def reveal_name(body: RevealNameIn,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    # 仅本行报关员有「脱敏后二次确认看全名」场景；企业管理员看自家企业无需留痕
    if user.role not in (Role.BROKER, Role.SUPERVISOR):
        raise BizError("仅本行报关员/监管员可申请查看企业全称，企业管理员请在本企业视图查看",
                       code="reveal_forbidden", status_code=403)
    ent = db.get(Enterprise, body.enterprise_id)
    if not ent:
        raise BizError("企业不存在", code="not_found", status_code=404)
    reason = body.reason.strip()
    if len(reason) < 10:
        raise BizError("二次确认未通过：查看企业全名必须填写不少于10个字的具体业务理由，留痕备查。",
                       code="reason_too_short", status_code=400)

    record = NameReveal(
        broker_id=user.id,
        broker_name=user.display_name,
        enterprise_id=ent.id,
        enterprise_name=ent.name_full,
        reason=reason,
    )
    db.add(record)
    db.commit()
    return {
        "ok": True,
        "enterprise_id": ent.id,
        "name_full": ent.name_full,
        "credit_code": ent.credit_code,
        "audit": {
            "viewer": user.display_name,
            "reason": reason,
            "time": record.created_at.isoformat(timespec="seconds"),
            "notice": "本次查看已写入合规留痕，监管员可随时审计。",
        },
    }


@router.get("/enterprises/reveals/audit")
def reveal_audit(enterprise_id: int | None = None,
                 user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if user.role == Role.ENTERPRISE:
        raise BizError("企业管理员无权访问本行留痕审计", code="forbidden", status_code=403)
    q = db.query(NameReveal)
    if user.role == Role.BROKER:
        q = q.filter(NameReveal.broker_id == user.id)
    if enterprise_id:
        q = q.filter(NameReveal.enterprise_id == enterprise_id)
    rows = q.order_by(NameReveal.id.desc()).limit(200).all()
    return [{
        "id": r.id,
        "broker_name": r.broker_name,
        "enterprise_id": r.enterprise_id,
        "enterprise_name": r.enterprise_name,
        "reason": r.reason,
        "created_at": r.created_at.isoformat(timespec="seconds"),
    } for r in rows]


# ---------- 委托合同 ----------

@router.get("/contracts")
def list_contracts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Contract)
    if user.role == Role.ENTERPRISE:
        q = q.filter(Contract.enterprise_id == user.enterprise_id)
    return [contract_out(c) for c in q.order_by(Contract.id.desc()).all()]


@router.post("/contracts")
def create_contract(body: ContractSignIn,
                    user: User = Depends(require_roles(Role.BROKER, Role.SUPERVISOR)),
                    db: Session = Depends(get_db)):
    ent = db.get(Enterprise, body.enterprise_id)
    if not ent:
        raise BizError("企业不存在，无法建立委托", code="enterprise_not_found", status_code=404)
    # 链路拦截 1：未备案企业不得签委托合同
    if ent.status != EnterpriseStatus.ACTIVE:
        label = "备案驳回" if ent.status == EnterpriseStatus.REJECTED else "备案中"
        raise BizError(
            f"企业「{ent.name_short}」当前状态为「{label}」，委托链路被拦下："
            f"必须先完成海关备案审核（状态=已备案），才能签委托合同。"
            + (f"驳回原因：{ent.reject_reason}。" if ent.reject_reason else ""),
            code="enterprise_not_filed",
        )
    existing = db.query(Contract).filter(
        Contract.enterprise_id == ent.id,
        Contract.status == ContractStatus.ACTIVE,
    ).first()
    if existing:
        raise BizError(f"企业「{ent.name_short}」已存在生效合同 {existing.contract_no}，"
                       f"请先终止原合同再续签，避免重复委托", code="active_contract_exists")

    seq = db.query(Contract).count() + 1
    contract = Contract(
        contract_no=f"WT-{datetime.utcnow().year}-{seq:04d}",
        enterprise_id=ent.id,
        scope_text=body.scope_text or "进出口货物报关申报全流程委托",
        status=ContractStatus.PENDING,
    )
    db.add(contract)
    db.commit()
    db.refresh(contract)
    return {"ok": True, "contract": contract_out(contract),
            "hint": "委托合同已拟妥，状态「待签署」，企业签署生效后方可立项报关单。"}


@router.post("/contracts/{contract_id}/sign")
def sign_contract(contract_id: int,
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    contract = db.get(Contract, contract_id)
    if not contract:
        raise BizError("合同不存在", code="not_found", status_code=404)
    if user.role == Role.ENTERPRISE and user.enterprise_id != contract.enterprise_id:
        raise BizError("不能签署其他企业的委托合同（企业间隔离拦截）",
                       code="cross_enterprise_forbidden", status_code=403)
    if user.role not in (Role.BROKER, Role.SUPERVISOR, Role.ENTERPRISE):
        raise BizError("当前角色无权签署委托合同", code="role_forbidden", status_code=403)
    if contract.status == ContractStatus.ACTIVE:
        raise BizError(f"合同 {contract.contract_no} 已生效，重复签署被拦截", code="already_signed")
    if contract.status == ContractStatus.TERMINATED:
        raise BizError(f"合同 {contract.contract_no} 已终止，不能再签署，请重新拟定合同",
                       code="contract_terminated")
    contract.status = ContractStatus.ACTIVE
    contract.signed_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "contract": contract_out(contract),
            "hint": "委托合同已生效，现在可以为该企业立项报关单并指派报关员。"}


@router.get("/users/brokers")
def list_brokers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(User).filter(User.role == Role.BROKER, User.active.is_(True)).all()
    return [user_out(u) for u in rows]
