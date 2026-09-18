"""预置数据：首次启动自动播种，开箱即见真实业务数据。

内容：
- 4 家进出口企业（3 家已备案 + 1 家备案中，用于演示委托链路拦截）
- 五类账号：报关员 / 海关审单员 / 企业管理员 / 监管员 / 场站查验员
- 19 张报关单，覆盖 委托中/已录入/审单中/查验中/已放行/已结关/已撤销
- 2 个监管场站、7 个查验车位（普通/冷链/重机）、4 名查验员（1 人请假中）
- 今日查验排期：链式改派场景 + 预置撞车/资质不符红单 + 全取消待重排单
- 6-9 月历史查验：8 月频繁改派，两口径及时率明显背离
- 征税红点（已逾期 / 3 日内到期 / 已缴）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .database import SessionLocal, Base, engine
from .models import (
    Role, User, Enterprise, Contract, Declaration, DeclarationEvent, Inspection,
    Yard, Bay, ReviewDecision, ScheduleLog,
    EnterpriseStatus, ContractStatus, DeclStatus,
)
from .security import hash_password
from .routers.clients import make_masked_name
from .services import scheduling as sched

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

        # ---------- 监管场站 / 车位 ----------
        yard_yt = Yard(code="YTN-YD", name="盐田综合监管场站", port="盐田港",
                       address="盐田港进港大道查验区", open_hour=8, close_hour=20, horizon_days=7)
        yard_sk = Yard(code="SKS-JG", name="蛇口机检监管场站", port="蛇口港",
                       address="蛇口集装箱码头配套查验区", open_hour=8, close_hour=18, horizon_days=7)
        db.add_all([yard_yt, yard_sk])
        db.flush()

        def mk_bay(yard, seq, code, name, tags):
            b = Bay(yard_id=yard.id, seq=seq, code=code, name=name, cert_tags=tags)
            db.add(b)
            return b

        b_yt_a1 = mk_bay(yard_yt, 1, "A-01", "A区查验台 1 号", "normal")
        b_yt_a2 = mk_bay(yard_yt, 2, "A-02", "A区查验台 2 号", "normal")
        b_yt_c1 = mk_bay(yard_yt, 3, "C-01", "冷链查验平台 1 号", "cold,food")
        b_yt_h1 = mk_bay(yard_yt, 4, "H-01", "重机查验位 1 号", "normal,heavy")
        b_sk_b12 = mk_bay(yard_sk, 1, "B-12", "B区查验台 12 号", "normal")
        b_sk_b13 = mk_bay(yard_sk, 2, "B-13", "B区查验台 13 号", "normal")
        b_sk_c2 = mk_bay(yard_sk, 3, "C-02", "冷链查验平台 2 号", "cold,food")
        db.flush()

        # ---------- 查验员 ----------
        def mk_inspector(username, display, yard, certs, available=True, reason=""):
            u = User(
                username=username, password_hash=hash_password(DEFAULT_PASSWORD),
                display_name=display, role=Role.INSPECTOR,
                yard_id=yard.id, inspector_certs=certs,
                available=available, unavailable_reason=reason,
                port=yard.port,
            )
            db.add(u)
            return u

        insp_sun = mk_inspector("sun_li", "孙丽（查验员）", yard_yt, "normal,cold,food")
        insp_zhou = mk_inspector("zhou_qiang", "周强（查验员）", yard_yt, "normal,heavy")
        insp_lin = mk_inspector("lin_kai", "林凯（查验员）", yard_sk, "normal,cold,food",
                                available=False, reason="家中事假，9月20日返岗")
        insp_zheng = mk_inspector("zheng_min", "郑敏（查验员）", yard_sk, "normal")
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

        seq = 0

        def make_decl(no, ent, contract, broker, status, ie, port, cargo, hs, qty, value,
                      tax=0.0, due_offset_days=None, paid=False, days_ago=10, remark=""):
            nonlocal seq
            seq += 1
            entrust = _dt(days_ago, 8)
            now = datetime.utcnow()
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
        d1 = make_decl("I2026090100001", e1, c1, broker_zhang, DeclStatus.CLOSED, "import", "盐田港",
                       "贴片电容 0402 系列", "8532241000", "2000000个", 1280000,
                       tax=166400, due_offset_days=-18, paid=True, days_ago=24,
                       remark="整柜进口，已结关归档")
        make_decl("E2026090800002", e1, c1, broker_zhang, DeclStatus.RELEASED, "export", "盐田港",
                  "智能网关主板", "8471504090", "12000片", 860000,
                  tax=0, due_offset_days=None, paid=False, days_ago=9)
        d3 = make_decl("I2026091200003", e1, c1, broker_zhang, DeclStatus.REVIEWING, "import", "盐田港",
                       "射频模组 RFM-7", "8517799000", "60000个", 2350000,
                       tax=305500, due_offset_days=-2, paid=False, days_ago=6,
                       remark="申报价格待复核；税款已逾期")
        make_decl("I2026091600004", e1, c1, broker_zhang, DeclStatus.ENTRUSTED, "import", "盐田港",
                  "电源管理 IC", "8542399000", "300000个", 620000,
                  days_ago=1, remark="客户刚委托，待录入")
        make_decl("E2026091500005", e1, c1, broker_li, DeclStatus.ENTERED, "export", "蛇口港",
                  "工业通讯模块", "8517629900", "8500台", 430000,
                  days_ago=2, remark="要素已录入，待提交审单")

        # —— 企业 2 远航 ——（精密机械）
        d6 = make_decl("I2026090500006", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "盐田港",
                       "五轴数控机床", "8457101000", "4台", 3680000,
                       tax=478400, due_offset_days=2, paid=False, days_ago=12,
                       remark="布控查验，今日到场开箱")
        make_decl("E202608200007", e2, c2, broker_li, DeclStatus.CLOSED, "export", "北仑港",
                  "精密轴承组件", "8482102000", "9000套", 540000,
                  tax=0, paid=False, days_ago=28)
        make_decl("I2026091400008", e2, c2, broker_li, DeclStatus.REVIEWING, "import", "蛇口港",
                  "液压伺服阀", "8481201000", "240台", 720000,
                  tax=93600, due_offset_days=1, paid=False, days_ago=4)

        # —— 企业 3 鲜驰 ——（冷链食品）
        d9 = make_decl("I2026090700009", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
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

        # —— 今日查验演示单 ——
        # 链式改派链：d14/d16 与锚点 d6 同车位同查验员，拖后锚点会逐级顺延
        d14 = make_decl("I2026091700014", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "盐田港",
                        "数控折弯机整机", "8462220000", "2台", 1450000, days_ago=1,
                        remark="今日排期，随重机位链式顺延演示")
        d15 = make_decl("I2026091800015", e1, c1, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "工业连接器", "8536900000", "120000个", 380000, days_ago=0)
        d16 = make_decl("I2026091800016", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "盐田港",
                        "液压泵站总成", "8413609010", "6台", 820000, days_ago=0)
        # 预置红单：同车位同时段两单 + 同查验员同时两单（甘特一打开就能看到冲突标红）
        d17 = make_decl("I2026091800017", e1, c1, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "贴片电阻阵列", "8533100000", "800万只", 260000, days_ago=0)
        d18 = make_decl("I2026091800018", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "盐田港",
                        "伺服驱动器", "8504409900", "900台", 540000, days_ago=0)
        # 排期全部取消、等待重新排期的单
        d19 = make_decl("I2026091600019", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "冷冻马鲛鱼排", "0304899090", "18吨", 430000, days_ago=2,
                        remark="两次排期均取消，等待重排")

        db.flush()

        # ---------- 逻辑审核结论（查验中的单均有 pass_inspect） ----------
        inspecting_decls = [d6, d9, d14, d15, d16, d17, d18, d19]
        for idx, d in enumerate(inspecting_decls):
            db.add(ReviewDecision(
                declaration_id=d.id, officer_id=customs_wang.id,
                officer_name=customs_wang.display_name, result="pass_inspect",
                document_ok=True, logic_ok=True,
                risk_tags="价格核查" if d is d6 else "",
                opinion=("单证一致、归类逻辑无误，命中布控，转查验。" if d is not d19
                         else "单证审核通过，前两次排期因场站原因取消，需重新安排。"),
                created_at=d.updated_at,
            ))
        # d3/d8 审单中不给结论 → 演示「未审核不能排期」拦截

        # ---------- 查验排期 ----------
        now = datetime.utcnow()

        def t_today(h, m=0):
            return now.replace(hour=h, minute=m, second=0, microsecond=0)

        def mk_insp(decl, yard, bay, inspector, start, end, status="scheduled",
                    due=None, finished=None, req=None, created=None, note=""):
            certs = req if req is not None else sched.infer_required_certs(decl.hs_code, decl.cargo_name)
            insp = Inspection(
                declaration_id=decl.id, yard_id=yard.id, bay_id=bay.id if bay else None,
                scheduled_at=start, scheduled_end=end,
                due_at=due or start, finished_at=finished,
                port=yard.port, bay=(bay.name if bay else ""),
                inspector_id=inspector.id if inspector else None,
                status=status, result_note=note,
                required_certs=",".join(certs), version=1,
                created_at=created or now - timedelta(days=1),
            )
            db.add(insp)
            db.flush()
            return insp

        def mk_log(insp, action, reason, detail=None, created=None, actor=None):
            db.add(ScheduleLog(
                inspection_id=insp.id, declaration_id=insp.declaration_id,
                actor_id=(actor or customs_wang).id,
                actor_name=(actor or customs_wang).display_name,
                action=action, reason=reason,
                detail=json.dumps(detail, ensure_ascii=False) if detail else "",
                created_at=created or datetime.utcnow(),
            ))

        # 锚点：五轴数控机床 10:30–12:30 重机位/周强；后续同资源单 13:00、14:30 排成链
        i6 = mk_insp(d6, yard_yt, b_yt_h1, insp_zhou,
                     t_today(10, 30), t_today(12, 30),
                     due=t_today(12, 30))
        mk_log(i6, "create", "布控当日到场开箱查验",
               {"at": t_today(10, 30).isoformat(timespec="minutes")},
               created=now - timedelta(days=1))
        i14 = mk_insp(d14, yard_yt, b_yt_h1, insp_zhou,
                      t_today(13, 0), t_today(14, 30), due=t_today(15, 0),
                      created=now - timedelta(hours=18))
        mk_log(i14, "create", "")
        i16 = mk_insp(d16, yard_yt, b_yt_h1, insp_zhou,
                      t_today(14, 30), t_today(16, 0), due=t_today(16, 30),
                      created=now - timedelta(hours=18))
        mk_log(i16, "create", "")
        # 链上不受影响的对照单：普通位/孙丽
        mk_insp(d15, yard_yt, b_yt_a1, insp_sun,
                t_today(15, 0), t_today(16, 30), due=t_today(17, 0),
                created=now - timedelta(hours=12))

        # —— 预置冲突红单（种子直接落入，甘特加载后按同一套规则标红）——
        # 冲突 1+2：A-02 09:00-11:00 与 09:30-11:30 同车位重叠；孙丽同时段还在 A-01 有一单
        i17 = mk_insp(d17, yard_yt, b_yt_a2, insp_sun,
                      t_today(9, 0), t_today(11, 0), due=t_today(11, 0),
                      created=now - timedelta(hours=20))
        mk_log(i17, "create", "")
        i18 = mk_insp(d18, yard_yt, b_yt_a2, insp_sun,
                      t_today(9, 30), t_today(11, 30), due=t_today(11, 30),
                      created=now - timedelta(hours=19))
        mk_log(i18, "create", "电话人工临时加塞（历史遗留冲突，待甘特纠偏）")
        # 冲突 3：冷链三文鱼被错排到普通位、且查验员无冷链/食品资质（逾期未到场）
        i9 = mk_insp(d9, yard_yt, b_yt_a1, insp_zhou,
                     now - timedelta(days=1, hours=2), now - timedelta(days=1),
                     status="scheduled",
                     due=now - timedelta(days=1),
                     req=["cold", "food"],
                     created=now - timedelta(days=3),
                     note="企业未按时到场，已两次催告")
        mk_log(i9, "create", "初次排期（后发现车位/资质不匹配，待改派）")

        # —— 全取消单：两次排期都取消 ——
        c_first = mk_insp(d19, yard_yt, b_yt_c1, insp_sun,
                          now - timedelta(days=2) - timedelta(hours=2),
                          now - timedelta(days=2),
                          status="cancelled", due=now - timedelta(days=2),
                          created=now - timedelta(days=3))
        mk_log(c_first, "cancel", "企业车队故障，货到不了场站，申请取消改期")
        c_second = mk_insp(d19, yard_yt, b_yt_c1, insp_sun,
                           now - timedelta(days=1, hours=3),
                           now - timedelta(days=1, hours=1),
                           status="cancelled", due=now - timedelta(days=1),
                           created=now - timedelta(days=2))
        mk_log(c_second, "cancel", "冷链平台临时检修无法温控，取消等待重排")

        # —— d1 已结关历史查验（按时完成）——
        mk_insp(d1, yard_yt, b_yt_a1, insp_sun,
                _dt(20, 14), _dt(20, 16), status="done",
                due=_dt(20, 16), finished=_dt(20, 14) + timedelta(hours=1, minutes=40),
                created=_dt(22), note="开箱核对品名数量无误")

        # 蛇口场站今日一单（查验员林凯请假中 → 演示人请假改派链）
        d20 = make_decl("I2026091800020", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "蛇口港",
                        "工业阀门铸件", "8481909000", "4200件", 260000, days_ago=0)
        db.add(ReviewDecision(
            declaration_id=d20.id, officer_id=customs_wang.id,
            officer_name=customs_wang.display_name, result="pass_inspect",
            document_ok=True, logic_ok=True, opinion="机检查验", created_at=d20.updated_at))
        i20 = mk_insp(d20, yard_sk, b_sk_b12, insp_lin,
                      t_today(10, 0), t_today(12, 0), due=t_today(12, 0),
                      created=now - timedelta(hours=10))
        mk_log(i20, "create", "")

        db.flush()

        # ---------- 近三个月历史查验（双口径及时率演示） ----------
        def month_start(year, month):
            return datetime(year, month, 1)

        def next_month(dt):
            return datetime(dt.year + (1 if dt.month == 12 else 0),
                            1 if dt.month == 12 else dt.month + 1, 1)

        hist_seq = 100

        def make_history_month(year, month, n, on_time, late, abnormal_late, cancelled_late):
            """在指定月份造 n 张已结关单 + 查验记录。
            on_time/late/abnormal_late 之和 + cancelled_late = n。
            晚完成单同步写改派留痕（频繁改派月份时效口径走低、单量口径仍高）。
            """
            nonlocal hist_seq
            mstart = month_start(year, month)
            mend = next_month(mstart)
            for k in range(n):
                hist_seq += 1
                day = 4 + (k * 2 % 20)
                entrust = mstart.replace(day=min(day, 26)) + timedelta(hours=8)
                is_cancel = k >= n - cancelled_late
                is_abn = (not is_cancel) and k >= n - cancelled_late - abnormal_late
                late_flag = (not is_cancel) and k >= on_time
                no = f"H{mstart.strftime('%Y%m')}{hist_seq:05d}"
                hd = Declaration(
                    decl_no=no, client_ref=f"REF-{no}",
                    enterprise_id=(e1, e2, e3)[hist_seq % 3].id,
                    contract_id=(c1, c2, c3)[hist_seq % 3].id,
                    broker_id=broker_zhang.id, customs_officer_id=customs_wang.id,
                    status=DeclStatus.CLOSED, version=5,
                    ie_type="import", port="盐田港",
                    cargo_name=("历史查验货-" + no), hs_code="8532241000",
                    qty="一批", total_value=100000 + k * 137, currency="CNY",
                    entrust_time=entrust, created_at=entrust, updated_at=mend - timedelta(days=1),
                )
                db.add(hd)
                db.flush()
                db.add(DeclarationEvent(
                    declaration_id=hd.id, actor_id=customs_wang.id,
                    actor_name=customs_wang.display_name,
                    from_status=None, to_status=DeclStatus.ENTRUSTED,
                    note="企业委托立项", created_at=entrust))
                db.add(ReviewDecision(
                    declaration_id=hd.id, officer_id=customs_wang.id,
                    officer_name=customs_wang.display_name, result="pass_inspect",
                    document_ok=True, logic_ok=True, opinion="历史月度统计单",
                    created_at=entrust + timedelta(hours=6)))
                # 应查验日 = 委托后第 2 天 18:00 截止；上午完成即按期
                due = (entrust + timedelta(days=2)).replace(hour=18, minute=0)
                start = due.replace(hour=9, minute=0)
                created = entrust + timedelta(hours=10)
                if is_cancel:
                    ci = Inspection(
                        declaration_id=hd.id, yard_id=yard_yt.id, bay_id=b_yt_a1.id,
                        scheduled_at=start, scheduled_end=start + timedelta(hours=2),
                        due_at=due, finished_at=None,
                        port="盐田港", bay=b_yt_a1.name, inspector_id=insp_sun.id,
                        status="cancelled", required_certs="normal", version=3,
                        created_at=created)
                    db.add(ci)
                    db.flush()
                    mk_log(ci, "create", "", created=created)
                    mk_log(ci, "cancel", "客户资料未齐，申请取消查验", created=created + timedelta(hours=3))
                    # 取消后补一场已完成查验，单据最终结关（派单口径分母仍含取消那单）
                    start2 = due + timedelta(days=2, hours=2)
                    ci2 = Inspection(
                        declaration_id=hd.id, yard_id=yard_yt.id, bay_id=b_yt_a2.id,
                        scheduled_at=start2, scheduled_end=start2 + timedelta(hours=2),
                        due_at=start2 + timedelta(hours=2),
                        finished_at=start2 + timedelta(hours=1, minutes=40),
                        port="盐田港", bay=b_yt_a2.name, inspector_id=insp_sun.id,
                        status="done", required_certs="normal", version=1,
                        created_at=due + timedelta(days=1))
                    db.add(ci2)
                    db.flush()
                    mk_log(ci2, "create", "取消后重新排期", created=due + timedelta(days=1))
                    mk_log(ci2, "finish", "重查无误", created=ci2.finished_at)
                    continue

                if late_flag:
                    # 频繁改派：计划从 due 当日一路后推，实际在 due 之后完成
                    actual_start = due + timedelta(days=3 if not is_abn else 5, hours=2)
                    finished = actual_start + timedelta(hours=1, minutes=50)
                    batch = f"RA-H{hist_seq:05d}"
                    hi = Inspection(
                        declaration_id=hd.id, yard_id=yard_yt.id,
                        bay_id=b_yt_h1.id if not is_abn else b_yt_a2.id,
                        scheduled_at=actual_start,
                        scheduled_end=actual_start + timedelta(hours=2),
                        due_at=due, finished_at=finished,
                        port="盐田港",
                        bay=(b_yt_h1.name if not is_abn else b_yt_a2.name),
                        inspector_id=insp_zhou.id if not is_abn else insp_sun.id,
                        status="abnormal" if is_abn else "done",
                        result_note=("改期后到场，开箱待复核" if is_abn else "查验无误"),
                        required_certs="normal", version=4,
                        reassign_batch_id=batch, created_at=created)
                    db.add(hi)
                    db.flush()
                    mk_log(hi, "create", "", created=created)
                    mk_log(hi, "reassign", "A区查验台升降平台故障，改重机位顺延",
                           {"reason_type": "bay_broken"}, created=due - timedelta(hours=4),
                           actor=sup_zhao)
                    mk_log(hi, "chain_move", "同场站后续单链式顺延",
                           {"batch": batch}, created=due - timedelta(hours=4), actor=sup_zhao)
                    mk_log(hi, "window_override", "企业车辆夜间到港，申请次日一早查验",
                           created=due + timedelta(days=1), actor=sup_zhao)
                    mk_log(hi, "finish", hi.result_note, created=finished)
                else:
                    finished = start + timedelta(hours=1, minutes=30)
                    hi = Inspection(
                        declaration_id=hd.id, yard_id=yard_yt.id, bay_id=b_yt_a1.id,
                        scheduled_at=start, scheduled_end=start + timedelta(hours=2),
                        due_at=due, finished_at=finished,
                        port="盐田港", bay=b_yt_a1.name, inspector_id=insp_sun.id,
                        status="done", result_note="按期查验无误",
                        required_certs="normal", version=1, created_at=created)
                    db.add(hi)
                    db.flush()
                    mk_log(hi, "create", "", created=created)
                    mk_log(hi, "finish", "按期查验无误", created=finished)

        # 6 月：6 单全部按期；7 月：8 单 7 按期 1 改派晚到；
        # 8 月（频繁改派）：10 单派出，2 按期 / 6 晚完成 / 1 异常晚完成 / 1 取消后补查
        make_history_month(2026, 6, 6, on_time=6, late=0, abnormal_late=0, cancelled_late=0)
        make_history_month(2026, 7, 8, on_time=7, late=1, abnormal_late=0, cancelled_late=0)
        make_history_month(2026, 8, 10, on_time=2, late=6, abnormal_late=1, cancelled_late=1)

        db.commit()
        log.info("预置数据完成：4 家企业 / 11 个账号 / 2 场站 7 车位 / 当月查验排期 + 6-8月历史双口径数据，"
                 "演示账号统一口令 %s", DEFAULT_PASSWORD)
    finally:
        db.close()
