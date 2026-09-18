"""请求依赖：从 Bearer token 解析当前登录用户，做企业间数据隔离。"""
from __future__ import annotations

from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, Role, Declaration, EnterpriseStatus, ContractStatus, BizError


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"code": "unauthorized", "reason": "未登录或登录已失效，请重新登录"})
    token = authorization.split(" ", 1)[1].strip()
    from .models import Session as SessionModel
    sess = db.query(SessionModel).filter(SessionModel.token == token).first()
    if not sess or sess.expires_at < datetime.utcnow():
        raise HTTPException(401, detail={"code": "unauthorized", "reason": "登录态已过期，请重新登录"})
    user = db.get(User, sess.user_id)
    if not user or not user.active:
        raise HTTPException(401, detail={"code": "unauthorized", "reason": "账号不可用，请联系管理员"})
    return user


def require_roles(*roles: Role):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, detail={
                "code": "role_forbidden",
                "reason": f"该功能仅向 {'、'.join(r.value for r in roles)} 开放",
            })
        return user
    return checker


def load_declaration_scoped(decl_id: int, user: User, db: Session) -> Declaration:
    """取报关单并做企业间隔离校验。

    隔离规则：
    - 企业管理员：只能看本企业的单，越权看他人单据 → 403 错误态（前端渲染错误页而非空白页）
    - 报关员/海关审单员/监管员：本行/海关视角可跨企业查看（监管必要），但操作仍受状态机角色约束
    """
    decl = db.get(Declaration, decl_id)
    if not decl:
        raise BizError(f"报关单 #{decl_id} 不存在，可能已被删除或编号有误",
                       code="not_found", status_code=404)
    if user.role == Role.ENTERPRISE and user.enterprise_id != decl.enterprise_id:
        raise BizError(
            "越权访问拦截：该报关单属于其他进出口企业，您所在企业无权查看。"
            "本次访问已按合规要求拒绝。如确需核对，请联系本行通过合规流程处理。",
            code="cross_enterprise_forbidden",
            status_code=403,
        )
    return decl


def ensure_active_contract(enterprise_id: int, contract_id: int, db: Session):
    """委托链路校验：企业必须已备案、合同必须已生效，且合同确属该企业。"""
    from .models import Enterprise, Contract
    ent = db.get(Enterprise, enterprise_id)
    if not ent:
        raise BizError("企业不存在，无法立项报关单", code="enterprise_not_found", status_code=404)
    if ent.status != EnterpriseStatus.ACTIVE:
        raise BizError(
            f"企业「{ent.name_short}」尚未完成备案（当前：备案中/被驳回），"
            f"备案通过前不得签署委托或立项报关单。请先在「客户与委托」完成企业备案。",
            code="enterprise_not_active",
        )
    contract = db.get(Contract, contract_id)
    if not contract or contract.enterprise_id != enterprise_id:
        raise BizError("委托合同与企业不匹配，不能凭非本企业合同立项", code="contract_mismatch", status_code=400)
    if contract.status != ContractStatus.ACTIVE:
        raise BizError(
            f"委托合同 {contract.contract_no} 尚未生效（当前：待签署/已终止），"
            f"必须先签署生效委托合同，报关单才能立项派单。",
            code="contract_not_active",
        )
    return ent, contract
