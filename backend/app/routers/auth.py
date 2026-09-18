"""登录 / 当前用户 / 登出。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User, Session as SessionModel, BizError
from ..config import settings
from ..security import hash_password, verify_password, new_token
from ..schemas import LoginIn, user_out
from ..deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip()).first()
    if not user or not verify_password(body.password, user.password_hash):
        # 统一报错，不区分「用户不存在/口令错误」，避免账号枚举
        raise HTTPException(401, detail={"code": "bad_credentials", "reason": "用户名或口令不正确"})
    if not user.active:
        raise HTTPException(403, detail={"code": "account_disabled", "reason": "账号已停用，请联系管理员"})

    token = new_token()
    db.add(SessionModel(
        token=token,
        user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(seconds=settings.TOKEN_TTL_SECONDS),
    ))
    db.commit()
    return {"token": token, "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.post("/logout")
def logout(user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    # 依赖里已经校验过 token，这里直接按 user 最近会话删除即可
    db.query(SessionModel).filter(SessionModel.user_id == user.id).delete()
    db.commit()
    return {"ok": True}
