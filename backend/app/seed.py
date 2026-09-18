"""预置数据：首次启动自动播种，开箱即见真实业务数据。

内容：
- 4 家进出口企业（3 家已备案 + 1 家备案中，用于演示委托链路拦截）
- 四类账号：报关员 / 海关审单员 / 企业管理员 / 监管员
- 12 张报关单，覆盖 委托中/已录入/审单中/查验中/已放行/已结关/已撤销
- 查验排期（含逾期任务）、征税红点（已逾期 / 3 日内到期 / 已缴）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .database import SessionLocal, Base, engine
from .models import (
    Role, User, Enterprise, Contract, Declaration, DeclarationEvent, Inspection,
    EnterpriseStatus, ContractStatus, DeclStatus,
)
from .security import hash_password
from .routers.clients import make_masked_name

log = logging.getLogger("baoguantong.seed")

DEFAULT_PASSWORD = "bgt123456"

# (code, 全称, 信用代码, 联系人, 电话, 口岸倾向)
ENTERPRISES = [
    ("4401960101", "深圳市华腾国际供应链有限公司", "91440300MA5HT10101", "陈志成", "13800010001"),
    ("3302960202", "宁波远航精密机械进出口有限公司", "91330200MA2YH20202", "林芳", "13800010002"),
    ("4401960303", "广州鲜驰冷链食品有限公司", "91440100MA5XC30303", "黄立", "13800010003"),
    # 备案中：用于演示「未备案不得签合同、不得立项」的拦截
    ("3502960404", "厦门鹭通电子科技有限公司", "91350200MA5LT40404", "吴敏", "13800010004"),
]


def _dt(days_ago: float, hours: int = 9) -> datetime:
    d = datetime.utcnow() - timedelta(days=days_ago)
    return d.replace(hour=hours, minute=0, second=0, microsecond=0)


def seed_if_empty() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        log.info("数据库为空，开始预置演示数据 …")

        # ---------- 企业 ----------
        ents = []
        for idx, (code, full, credit, person, phone) in enumerate(ENTERPRISES):
            status = EnterpriseStatus.FILING if idx == 3 else EnterpriseStatus.ACTIVE
            ent = Enterprise(
                code=code,
                name_full=full,
                name_short=make_masked_name(full, code),
                credit_code=credit,
                contact_person=person,
                contact_phone=phone,
                ie_flag="进出口",
                status=status,
                filed_at=_dt(120 + idx) if status == EnterpriseStatus.ACTIVE else None,
                created_at=_dt(125 + idx),
            )
            db.add(ent)
            ents.append(ent)
        db.flush()
        e1, e2, e3, e4 = ents

        # ---------- 账号 ----------
        def mk_user(username, display, role, ent=None, port=""):
            u = User(
                username=username,
                password_hash=hash_password(DEFAULT_PASSWORD),
                display_name=display,
                role=role,
                enterprise_id=ent.id if ent else None,
                port=port,
            )
            db.add(u)
            return u

        broker_zhang = mk_user("zhang_wei", "张伟（报关员）", Role.BROKER, port="盐田港")
        broker_li = mk_user("li_na", "李娜（报关员）", Role.BROKER, port="蛇口港")
        customs_wang = mk_user("wang_hua", "王华（海关审单员）", Role.CUSTOMS, port="深圳海关")
        sup_zhao = mk_user("zhao_jing", "赵静（监管员）", Role.SUPERVISOR)
        mk_user("admin_huateng", "陈志成（华腾企业管理员）", Role.ENTERPRISE, ent=e1)
        mk_user("admin_yuanhang", "林芳（远航企业管理员）", Role.ENTERPRISE, ent=e2)
        mk_user("admin_xianchi", "黄立（鲜驰企业管理员）", Role.ENTERPRISE, ent=e3)
        db.flush()

        # ---------- 委托合同 ----------
        contracts = []
        for i, ent in enumerate((e1, e2, e3), start=1):
            c = Contract(
                contract_no=f"WT-2026-{i:04d}",
                enterprise_id=ent.id,
                scope_text="进出口货物报关申报、配合查验、代缴税费全流程委托",
                status=ContractStatus.ACTIVE,
                signed_at=_dt(110 - i),
                created_at=_dt(115 - i),
            )
            db.add(c)
            contracts.append(c)
        # 备案中企业的「待签」合同：用于演示链路第二步拦截（备案不过 → 合同签不了）
        c4 = Contract(
            contract_no="WT-2026-0004",
            enterprise_id=e4.id,
            status=ContractStatus.PENDING,
            created_at=_dt(2),
        )
        db.add(c4)
        db.flush()

        # ---------- 报关单 ----------
        # 状态主路径（事件序列）
        PATH = {
            DeclStatus.ENTERED: [("enter", DeclStatus.ENTRUSTED, DeclStatus.ENTERED, "完成报关单要素录入")],
            DeclStatus.REVIEWING: [
                ("enter", DeclStatus.ENTRUSTED, DeclStatus.ENTERED, "完成报关单要素录入"),
                ("submit_review", DeclStatus.ENTERED, DeclStatus.REVIEWING, "提交海关审单"),
            ],
            DeclStatus.INSPECTING: [
                ("enter", DeclStatus.ENTRUSTED, DeclStatus.ENTERED, "完成报关单要素录入"),
                ("submit_review", DeclStatus.ENTERED, DeclStatus.REVIEWING, "提交海关审单"),
                ("inspect", DeclStatus.REVIEWING, DeclStatus.INSPECTING, "审单布控，转查验"),
            ],
            DeclStatus.RELEASED: [
                ("enter", DeclStatus.ENTRUSTED, DeclStatus.ENTERED, "完成报关单要素录入"),
                ("submit_review", DeclStatus.ENTERED, DeclStatus.REVIEWING, "提交海关审单"),
                ("release_from_review", DeclStatus.REVIEWING, DeclStatus.RELEASED, "审单审结，无查验放行"),
            ],
            DeclStatus.CLOSED: [
                ("enter", DeclStatus.ENTRUSTED, DeclStatus.ENTERED, "完成报关单要素录入"),
                ("submit_review", DeclStatus.ENTERED, DeclStatus.REVIEWING, "提交海关审单"),
                ("inspect", DeclStatus.REVIEWING, DeclStatus.INSPECTING, "审单布控，转查验"),
                ("release_from_inspection", DeclStatus.INSPECTING, DeclStatus.RELEASED, "查验无误，放行"),
                ("close", DeclStatus.RELEASED, DeclStatus.CLOSED, "办结结关手续"),
            ],
            DeclStatus.CANCELLED: [
                ("cancel_entrusted", DeclStatus.ENTRUSTED, DeclStatus.CANCELLED, "企业取消委托，资料有误重新发起"),
            ],
        }

        now = datetime.utcnow()
        seq = 0

        def make_decl(no, ent, contract, broker, status, ie, port, cargo, hs, qty, value,
                      tax=0.0, due_offset_days=None, paid=False, days_ago=10, remark=""):
            nonlocal seq
            seq += 1
            entrust = _dt(days_ago, 8)
            decl = Declaration(
                decl_no=no,
                client_ref=f"REF-{no}",
                enterprise_id=ent.id,
                contract_id=contract.id,
                broker_id=broker.id,
                customs_officer_id=customs_wang.id,
                status=status,
                version=1,
                ie_type=ie,
                port=port,
                cargo_name=cargo,
                hs_code=hs,
                qty=qty,
                total_value=value,
                currency="CNY",
                tax_amount=tax,
                tax_due_date=(now + timedelta(days=due_offset_days)).replace(microsecond=0)
                if due_offset_days is not None else None,
                tax_paid=paid,
                tax_paid_at=_dt(days_ago - 2) if paid else None,
                remark=remark,
                entrust_time=entrust,
                created_at=entrust,
                updated_at=entrust,
            )
            db.add(decl)
            db.flush()

            # 立项事件
            db.add(DeclarationEvent(
                declaration_id=decl.id, actor_id=broker.id, actor_name=broker.display_name,
                from_status=None, to_status=DeclStatus.ENTRUSTED,
                note="企业委托立项", created_at=entrust,
            ))
            cur_time = entrust
            if status in PATH:
                for action, frm, to, note in PATH[status]:
                    cur_time += timedelta(hours=5)
                    actor = customs_wang if to in (DeclStatus.REVIEWING, DeclStatus.INSPECTING,
                                                   DeclStatus.RELEASED, DeclStatus.CLOSED) \
                        and frm != DeclStatus.ENTERED else broker
                    db.add(DeclarationEvent(
                        declaration_id=decl.id, actor_id=actor.id, actor_name=actor.display_name,
                        from_status=frm, to_status=to, note=note, created_at=cur_time,
                    ))
                    decl.version += 1
            if status == DeclStatus.ENTRUSTED:
                db.add(DeclarationEvent(
                    declaration_id=decl.id, actor_id=sup_zhao.id, actor_name=sup_zhao.display_name,
                    from_status=DeclStatus.ENTRUSTED, to_status=DeclStatus.ENTRUSTED,
                    note=f"派单给报关员 {broker.display_name}", created_at=cur_time + timedelta(hours=1),
                ))
            decl.updated_at = cur_time
            return decl

        c1, c2, c3 = contracts

        # —— 企业 1 华腾 ——（进口电子料为主）
        make_decl("I2026090100001", e1, c1, broker_zhang, DeclStatus.CLOSED, "import", "盐田港",
                  "贴片电容 0402 系列", "8532241000", "2000000个", 1280000,
                  tax=166400, due_offset_days=-18, paid=True, days_ago=24,
                  remark="整柜进口，已结关归档")
        make_decl("E2026090800002", e1, c1, broker_zhang, DeclStatus.RELEASED, "export", "盐田港",
                  "智能网关主板", "8471504090", "12000片", 860000,
                  tax=0, due_offset_days=None, paid=False, days_ago=9)
        make_decl("I2026091200003", e1, c1, broker_zhang, DeclStatus.REVIEWING, "import", "盐田港",
                  "射频模组 RFM-7", "8517799000", "60000个", 2350000,
                  # 逾期未缴税：作战台红色告警
                  tax=305500, due_offset_days=-2, paid=False, days_ago=6,
                  remark="申报价格待复核；税款已逾期")
        make_decl("I2026091600004", e1, c1, broker_zhang, DeclStatus.ENTRUSTED, "import", "盐田港",
                  "电源管理 IC", "8542399000", "300000个", 620000,
                  days_ago=1, remark="客户刚委托，待录入")
        make_decl("E2026091500005", e1, c1, broker_li, DeclStatus.ENTERED, "export", "蛇口港",
                  "工业通讯模块", "8517629900", "8500台", 430000,
                  days_ago=2, remark="要素已录入，待提交审单")

        # —— 企业 2 远航 ——（精密机械）
        make_decl("I2026090500006", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "蛇口港",
                  "五轴数控机床", "8457101000", "4台", 3680000,
                  tax=478400, due_offset_days=2, paid=False, days_ago=12,
                  remark="布控查验，今日到场开箱")
        make_decl("E2026082000007", e2, c2, broker_li, DeclStatus.CLOSED, "export", "北仑港",
                  "精密轴承组件", "8482102000", "9000套", 540000,
                  tax=0, paid=False, days_ago=28)
        make_decl("I2026091400008", e2, c2, broker_li, DeclStatus.REVIEWING, "import", "蛇口港",
                  "液压伺服阀", "8481201000", "240台", 720000,
                  # 3 日内到期：黄色红点预警
                  tax=93600, due_offset_days=1, paid=False, days_ago=4)

        # —— 企业 3 鲜驰 ——（冷链食品）
        make_decl("I2026090700009", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                  "冻切三文鱼（冷链）", "0304810000", "26吨", 980000,
                  tax=127400, due_offset_days=-5, paid=False, days_ago=10,
                  remark="查验逾期未到场，冷柜滞港费累计中")
        make_decl("E2026091600010", e3, c3, broker_zhang, DeclStatus.ENTERED, "export", "南沙港",
                  "速冻调味小龙虾", "1605400000", "18吨", 216000,
                  days_ago=1)
        make_decl("I2026091000011", e3, c3, broker_li, DeclStatus.RELEASED, "import", "盐田港",
                  "冷冻牛腩", "0202300090", "32吨", 560000,
                  tax=72800, due_offset_days=6, paid=False, days_ago=7)
        make_decl("E2026091700012", e3, c3, broker_li, DeclStatus.ENTRUSTED, "export", "南沙港",
                  "预制菜礼盒（年货试单）", "2106909090", "5000箱", 175000,
                  days_ago=0, remark="今日新委托")
        make_decl("I2026091300013", e2, c2, broker_li, DeclStatus.CANCELLED, "import", "蛇口港",
                  "二手注塑机（企业撤单）", "8477101090", "2台", 180000,
                  days_ago=5, remark="HS 归类存疑，企业撤回重新归类后再报")

        db.flush()

        # ---------- 查验排期 ----------
        d6 = db.query(Declaration).filter_by(decl_no="I2026090500006").first()
        d9 = db.query(Declaration).filter_by(decl_no="I2026090700009").first()
        d1 = db.query(Declaration).filter_by(decl_no="I2026090100001").first()
        today = now.replace(hour=10, minute=30, second=0, microsecond=0)

        db.add(Inspection(
            declaration_id=d6.id,
            scheduled_at=today + timedelta(hours=2),
            port="蛇口港", bay="B区查验台 12 号",
            inspector_id=customs_wang.id, status="pending",
            created_at=now - timedelta(days=1),
        ))
        # 逾期未完成的查验（作战台红点）
        db.add(Inspection(
            declaration_id=d9.id,
            scheduled_at=now - timedelta(days=1, hours=2),
            port="盐田港", bay="冷链查验平台 3 号",
            inspector_id=customs_wang.id, status="pending",
            result_note="企业未按时到场，已两次催告",
            created_at=now - timedelta(days=3),
        ))
        # 已完成的历史查验
        db.add(Inspection(
            declaration_id=d1.id,
            scheduled_at=_dt(20, 14),
            port="盐田港", bay="A区查验台 5 号",
            inspector_id=customs_wang.id, status="done",
            result_note="开箱核对品名数量无误",
            created_at=_dt(22),
        ))

        db.commit()
        log.info("预置数据完成：4 家企业 / 7 个账号 / 12 张报关单 / 3 条查验排期，"
                 "演示账号统一口令 %s", DEFAULT_PASSWORD)
    finally:
        db.close()
