"""预置数据：首次启动自动播种，开箱即见真实业务数据。

内容：
- 4 家进出口企业（3 家已备案 + 1 家备案中，用于演示委托链路拦截）
- 报关员 / 海关审单员 / 企业管理员 / 监管员 / 4 名带资质查验员
- 3 个监管场站（盐田/蛇口/南沙），普货/冷链/危化/大型设备台位，含故障台位与请假查验员
- 报关单覆盖 委托中/已录入/审单中/查验中/已放行/已结关/已撤销
- 本周查验甘特：逾期任务、同车位同查验员链（演示改派链式顺延）、故障车位、请假查验员
- 南沙场站排期全部取消、0018 单无有效排期（三种空态/错误态演示数据）
- 近 3 个月历史查验：8 月频繁改派，两口径方向相反（完成率高 / 准点率低）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .database import SessionLocal, Base, engine
from .models import (
    Role, User, Enterprise, Contract, Declaration, DeclarationEvent, Inspection,
    Yard, Bay, ScheduleChange,
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
        def mk_user(username, display, role, ent=None, port="", quals="", on_leave=False):
            u = User(
                username=username,
                password_hash=hash_password(DEFAULT_PASSWORD),
                display_name=display,
                role=role,
                enterprise_id=ent.id if ent else None,
                port=port,
                qualifications=quals,
                on_leave=on_leave,
            )
            db.add(u)
            return u

        broker_zhang = mk_user("zhang_wei", "张伟（报关员）", Role.BROKER, port="盐田港")
        broker_li = mk_user("li_na", "李娜（报关员）", Role.BROKER, port="蛇口港")
        customs_wang = mk_user("wang_hua", "王华（海关审单员）", Role.CUSTOMS, port="深圳海关")
        sup_zhao = mk_user("zhao_jing", "赵静（监管员）", Role.SUPERVISOR)
        # 查验员：资质不同，何军请假中（人请假改派场景）
        insp_zhou = mk_user("zhou_min", "周敏（查验员）", Role.INSPECTOR, port="盐田港", quals="chilled")
        insp_wu = mk_user("wu_tao", "吴涛（查验员）", Role.INSPECTOR, port="盐田港", quals="chilled,dg")
        insp_zheng = mk_user("zheng_kai", "郑凯（查验员）", Role.INSPECTOR, port="蛇口港", quals="large,chilled")
        insp_he = mk_user("he_jun", "何军（查验员）", Role.INSPECTOR, port="盐田港", quals="chilled", on_leave=True)
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

        # ---------- 监管场站 / 车位 ----------
        yt = Yard(name="盐田港监管查验场", port="盐田港", open_hour=8.5, close_hour=17.5, work_weekend=False)
        sk = Yard(name="蛇口港监管查验场", port="蛇口港", open_hour=9.0, close_hour=18.0, work_weekend=False)
        ns = Yard(name="南沙港监管查验场", port="南沙港", open_hour=8.5, close_hour=17.0, work_weekend=False)
        db.add_all([yt, sk, ns])
        db.flush()

        def mk_bay(yard, code, name, kind, sort_no, broken=False, note=""):
            b = Bay(yard_id=yard.id, code=code, name=name, kind=kind, sort_no=sort_no,
                    out_of_service=broken, note=note)
            db.add(b)
            return b

        b_yt_a05 = mk_bay(yt, "A-05", "A区普货查验台 5 号", "general", 1)
        # A-12 故障：车故障改派场景（当前有单 d3 排在这里）
        b_yt_a12 = mk_bay(yt, "A-12", "A区普货查验台 12 号", "general", 2,
                          broken=True, note="升降平台液压故障，9/17 起停用待修")
        b_yt_cc03 = mk_bay(yt, "CC-03", "冷链查验平台 3 号", "chilled", 3)
        b_yt_dg01 = mk_bay(yt, "DG-01", "危化查验专区 1 号", "dg", 4)
        b_yt_lg01 = mk_bay(yt, "LG-01", "大型设备台位 1 号", "large", 5)
        b_sk_b12 = mk_bay(sk, "B-12", "B区普货查验台 12 号", "general", 1)
        b_sk_cc07 = mk_bay(sk, "CC-07", "冷链查验平台 7 号", "chilled", 2)
        b_sk_lg02 = mk_bay(sk, "LG-02", "大型设备台位 2 号", "large", 3)
        b_ns_01 = mk_bay(ns, "NS-01", "南沙普货查验台 1 号", "general", 1)
        b_ns_02 = mk_bay(ns, "NS-02", "南沙普货查验台 2 号", "general", 2,
                         broken=True, note="吊具损坏，暂停使用")
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

        now = datetime.utcnow()

        def make_decl(no, ent, contract, broker, status, ie, port, cargo, hs, qty, value,
                      tax=0.0, due_offset_days=None, paid=False, days_ago=10, remark=""):
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
        # d3 审单布控后转查验中：排期落在故障车位 A-12（车故障改派演示）
        d3 = make_decl("I2026091200003", e1, c1, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                       "射频模组 RFM-7", "8517799000", "60000个", 2350000,
                       tax=305500, due_offset_days=-2, paid=False, days_ago=6,
                       remark="申报价格待复核；税款已逾期；布控查验")
        make_decl("I2026091600004", e1, c1, broker_zhang, DeclStatus.ENTRUSTED, "import", "盐田港",
                  "电源管理 IC", "8542399000", "300000个", 620000,
                  days_ago=1, remark="客户刚委托，待录入")
        make_decl("E2026091500005", e1, c1, broker_li, DeclStatus.ENTERED, "export", "蛇口港",
                  "工业通讯模块", "8517629900", "8500台", 430000,
                  days_ago=2, remark="要素已录入，待提交审单")

        # —— 企业 2 远航 ——（精密机械）
        d6 = make_decl("I2026090500006", e2, c2, broker_li, DeclStatus.INSPECTING, "import", "蛇口港",
                       "五轴数控机床", "8457101000", "4台", 3680000,
                       tax=478400, due_offset_days=2, paid=False, days_ago=12,
                       remark="布控查验，大型设备台位")
        make_decl("E2026082000007", e2, c2, broker_li, DeclStatus.CLOSED, "export", "北仑港",
                  "精密轴承组件", "8482102000", "9000套", 540000,
                  tax=0, paid=False, days_ago=28)
        # d8 保留审单中：安排查验弹窗演示（普货，蛇口）
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
        d11 = make_decl("I202609100011", e3, c3, broker_li, DeclStatus.RELEASED, "import", "盐田港",
                        "冷冻牛腩", "0202300090", "32吨", 560000,
                        tax=72800, due_offset_days=6, paid=False, days_ago=7)
        make_decl("E2026091700012", e3, c3, broker_li, DeclStatus.ENTRUSTED, "export", "南沙港",
                  "预制菜礼盒（年货试单）", "2106909090", "5000箱", 175000,
                  days_ago=0, remark="今日新委托")
        make_decl("I2026091300013", e2, c2, broker_li, DeclStatus.CANCELLED, "import", "蛇口港",
                  "二手注塑机（企业撤单）", "8477101090", "2台", 180000,
                  days_ago=5, remark="HS 归类存疑，企业撤回重新归类后再报")

        # —— 本期新增：查验甘特演示单 ——
        # 冷链链：0014、0015 今天排在 CC-03，周敏/何军（何军请假中）；与逾期的 d9 构成改派顺延链
        d14 = make_decl("I2026091700014", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "冻去骨牛肉（冷链）", "0202300090", "24吨", 860000,
                        tax=111800, days_ago=1, remark="冷链柜，今日上午查验")
        d15 = make_decl("I2026091700015", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "冻南美白虾（冷链）", "0306172000", "18吨", 540000,
                        days_ago=1, remark="冷链柜，原排查验员何军请假，待改派")
        # 危化品：周三已查完待放行（周甘特里的已完成条）
        d16 = make_decl("I2026091600016", e1, c1, broker_zhang, DeclStatus.INSPECTING, "import", "盐田港",
                        "锂电池电解液（易燃）", "2932190090", "320桶", 720000,
                        tax=93600, days_ago=3, remark="危化品，已查验待放行")
        # 南沙：布控后两次排期均取消 → 该单当前无有效排期 + 南沙场站排期全取消
        d18 = make_decl("I2026091500018", e3, c3, broker_zhang, DeclStatus.INSPECTING, "import", "南沙港",
                        "冻烤鱼片（冷链）", "0304899000", "12吨", 360000,
                        days_ago=3, remark="场站台位不匹配，两次排期取消待重新安排")

        db.flush()

        # ---------- 本周查验甘特 ----------
        def at(day_offset, h, m=0):
            d = (now + timedelta(days=day_offset))
            return d.replace(hour=h, minute=m, second=0, microsecond=0)

        def mk_insp(decl, yard, bay, inspector, start, minutes=120, status="pending",
                    due=None, result="", cancel_reason="", created_days_ago=None,
                    reassign_count=0, started=None, finished=None):
            i = Inspection(
                declaration_id=decl.id, yard_id=yard.id, bay_id=bay.id,
                inspector_id=inspector.id if inspector else None,
                scheduled_at=start, scheduled_end=start + timedelta(minutes=minutes),
                duration_minutes=minutes,
                required_qual=None,  # 下面按货补
                due_date=due or start,
                status=status, result_note=result, cancel_reason=cancel_reason,
                reassign_count=reassign_count,
                started_at=started, finished_at=finished,
                created_at=now - timedelta(days=created_days_ago or 0),
            )
            from .models import qual_for_cargo
            i.required_qual = qual_for_cargo(decl.hs_code, decl.cargo_name)
            db.add(i)
            return i

        # d9：昨天 09:00 逾期未查（冷链，周敏，CC-03）——改派后触发 0014→0015 链式顺延
        i_d9 = mk_insp(d9, yt, b_yt_cc03, insp_zhou, at(-1, 9), 120,
                       result="企业未按时到场，已两次催告", created_days_ago=3)
        # d6：今天 10:30 蛇口大型设备台位，郑凯
        mk_insp(d6, sk, b_sk_lg02, insp_zheng, at(0, 10, 30), 150, created_days_ago=1)
        # d3：今天 13:30 故障车位 A-12 ——车故障改派
        i_d3 = mk_insp(d3, yt, b_yt_a12, insp_zhou, at(0, 13, 30), 90, created_days_ago=1)
        # 冷链链两条
        i_d14 = mk_insp(d14, yt, b_yt_cc03, insp_zhou, at(0, 9), 120, reassign_count=1,
                        created_days_ago=1)
        i_d15 = mk_insp(d15, yt, b_yt_cc03, insp_he, at(0, 11), 120,
                        created_days_ago=1)  # 何军请假 → 人请假改派
        # d16：周三 10:00 危化已查完
        mk_insp(d16, yt, b_yt_dg01, insp_wu, at(-2, 10), 100, status="done",
                result="开箱核对品名数量无误，包装合规", started=at(-2, 10), finished=at(-2, 11, 45))
        # d11：8 天前已完成（已放行，锁定屏障数据）
        mk_insp(d11, yt, b_yt_cc03, insp_zhou, _dt(7, 10), 120, status="done",
                result="冷链温度记录完整，货证相符", started=_dt(7, 10), finished=_dt(7, 11) + timedelta(minutes=50),
                created_days_ago=8)
        # d1：20 天前已完成结关
        mk_insp(db.query(Declaration).filter_by(decl_no="I2026090100001").first(),
                yt, b_yt_a05, insp_zhou, _dt(20, 14), 120, status="done",
                result="开箱核对品名数量无误", started=_dt(20, 14), finished=_dt(20, 15) + timedelta(minutes=40),
                created_days_ago=22)

        # 南沙：0018 两次排期全取消（该单无有效排期 / 场站排期全取消）
        mk_insp(d18, ns, b_ns_01, insp_zheng, at(-4, 10), 120, status="cancelled",
                cancel_reason="南沙场站无冷链查验台位，资质不匹配，取消转盐田安排", created_days_ago=4)
        mk_insp(d18, ns, b_ns_02, insp_zheng, at(-1, 14), 120, status="cancelled",
                cancel_reason="NS-02 吊具损坏故障，企业车辆无法到场，再次取消", created_days_ago=2)
        db.flush()

        # 本周排期留痕
        def chg(insp, actor, ctype, reason, detail, days_ago=0):
            db.add(ScheduleChange(
                inspection_id=insp.id if insp else None,
                declaration_id=insp.declaration_id if insp else None,
                yard_id=insp.yard_id if insp else yt.id,
                actor_id=actor.id, actor_name=actor.display_name,
                change_type=ctype, reason=reason, detail=detail,
                created_at=now - timedelta(days=days_ago),
            ))

        import json as _json
        chg(i_d9, customs_wang, "schedule", "审单布控，安排查验",
            _json.dumps({"bay": "CC-03", "scheduled_at": at(-1, 9).isoformat()}, ensure_ascii=False), 3)
        chg(i_d14, customs_wang, "drag", "企业车辆晚到，拖拽后移 1 小时",
            _json.dumps({"old": "08:00", "new": "09:00"}, ensure_ascii=False), 1)
        chg(i_d3, sup_zhao, "bay_broken", "A-12 升降平台液压故障，9/17 起停用",
            _json.dumps({"bay": "A-12", "out_of_service": True, "affected_inspection_ids": [i_d3.id]},
                        ensure_ascii=False))
        db.flush()

        # ---------- 近 3 月历史查验（及时率双口径） ----------
        # 8 月频繁改派：单最终都查完（完成率高），但实际完成日普遍晚于应查验日（准点率低）
        hist_cargo = [
            ("工业阀门", "8481804090", "general"), ("贴片电阻", "8533100000", "general"),
            ("冻猪副制品", "0206490000", "chilled"), ("服务器电源模块", "8504409999", "general"),
            ("工业胶粘剂（易燃）", "3506910000", "general"), ("冻鱿鱼圈", "0307490000", "chilled"),
            ("精密导轨", "8466940000", "general"), ("冻鸡爪", "0207142200", "chilled"),
            ("光纤连接器", "8536700000", "general"), ("焊接用保护气体", "2804290000", "general"),
        ]
        brokers = [broker_zhang, broker_li]
        ent_contract = [(e1, c1), (e2, c2), (e3, c3)]

        def hist_decl(idx, ent, contract, broker, cargo, hs, when: datetime, status):
            no = f"I{when:%Y%m}{idx:05d}"
            d = Declaration(
                decl_no=no, client_ref=f"REF-{no}", enterprise_id=ent.id, contract_id=contract.id,
                broker_id=broker.id, customs_officer_id=customs_wang.id,
                status=status, version=4, ie_type="import", port="盐田港",
                cargo_name=cargo, hs_code=hs, qty="1批", total_value=200000 + idx * 137,
                currency="CNY", entrust_time=when - timedelta(days=5),
                created_at=when - timedelta(days=5), updated_at=when,
            )
            db.add(d)
            db.flush()
            return d

        def hist_month(year, month, count, on_time_n, late_days, reassign_each, start_idx,
                       late_first=False, day_max=26):
            """生成 count 条当月已完成查验：on_time_n 条准点，其余晚 late_days 天。

            late_first=True 时晚点单排在月初（当月未结束，保证完成日不落到未来）。
            """
            from .models import qual_for_cargo
            for k in range(count):
                cargo, hs, kind = hist_cargo[(start_idx + k) % len(hist_cargo)]
                ent, contract = ent_contract[(start_idx + k) % 3]
                broker = brokers[(start_idx + k) % 2]
                # 应查验日均匀分布在月内 3..day_max 号（当月未结束时 day_max 取过去日期）
                day = 3 + (k * (day_max - 3) // max(count - 1, 1))
                due = datetime(year, month, min(day, day_max), 10, 0)
                on_time = (k >= count - on_time_n) if late_first else (k < on_time_n)
                late = 0 if on_time else late_days[k % len(late_days)]
                finished = due + timedelta(days=late, hours=1, minutes=40)
                status = DeclStatus.CLOSED if finished < now - timedelta(days=2) else DeclStatus.RELEASED
                d = hist_decl(start_idx + k, ent, contract, broker, cargo, hs, due, status)
                if kind == "chilled":
                    bay, inspector = b_yt_cc03, insp_zhou
                elif hs[:2] in ("28", "29"):
                    bay, inspector = b_yt_dg01, insp_wu
                else:
                    bay, inspector = b_yt_a05, insp_zhou
                qual = qual_for_cargo(hs, cargo)
                db.add(Inspection(
                    declaration_id=d.id, yard_id=yt.id, bay_id=bay.id, inspector_id=inspector.id,
                    scheduled_at=due, scheduled_end=due + timedelta(minutes=120),
                    duration_minutes=120, required_qual=qual, due_date=due,
                    status="done", result_note="查验无误" if on_time else f"改派后于 {finished:%m-%d} 完成查验",
                    started_at=finished - timedelta(hours=2), finished_at=finished,
                    reassign_count=0 if on_time else reassign_each,
                    created_at=due - timedelta(days=4),
                ))
                if not on_time:
                    batch = f"CHAIN-HIST-{year}{month}-{start_idx + k}"
                    for r in range(reassign_each):
                        db.add(ScheduleChange(
                            inspection_id=None, declaration_id=d.id, yard_id=yt.id,
                            actor_id=customs_wang.id, actor_name=customs_wang.display_name,
                            change_type="chain_shift" if r else "reassign",
                            reason="台风天气场地调整，链式顺延" if r else "查验员请假/车位冲突改派",
                            detail=_json.dumps({"caused_by_decl": d.decl_no, "late_days": late}, ensure_ascii=False),
                            chain_batch=batch,
                            created_at=due - timedelta(days=3 - r),
                        ))
                db.flush()

        # 7 月：8 单全部准点、零改派 —— 两口径都 100%
        hist_month(2026, 7, 8, on_time_n=8, late_days=[0], reassign_each=0, start_idx=100)
        # 8 月：10 单全部完成（完成率 100%），但仅 3 单准点（准点率 30%），每单改派 2~3 次
        hist_month(2026, 8, 10, on_time_n=3, late_days=[2, 3, 4, 3], reassign_each=3, start_idx=200)
        # 9 月（截至昨日）：8 单中 6 单完成（5 准点 1 晚），2 单仍待查
        hist_month(2026, 9, 6, on_time_n=5, late_days=[2], reassign_each=1,
                   start_idx=300, late_first=True, day_max=15)
        # 9 月两条 pending（应查验日已过，未完成）
        for k in range(2):
            cargo, hs, kind = hist_cargo[(306 + k) % len(hist_cargo)]
            ent, contract = ent_contract[(306 + k) % 3]
            due = datetime(2026, 9, 10 + k * 3, 10, 0)
            d = hist_decl(306 + k, ent, contract, broker_zhang, cargo, hs, due, DeclStatus.INSPECTING)
            db.add(Inspection(
                declaration_id=d.id, yard_id=yt.id, bay_id=b_yt_a05.id, inspector_id=insp_zhou.id,
                scheduled_at=due, scheduled_end=due + timedelta(minutes=120),
                duration_minutes=120, required_qual=None, due_date=due,
                status="pending", reassign_count=1, created_at=due - timedelta(days=3),
            ))
        db.flush()

        db.commit()
        log.info("预置数据完成：4 家企业 / 11 个账号（含 4 名查验员）/ 3 个监管场站 10 个车位 / "
                 "本周查验甘特 + 近 3 月历史查验，演示账号统一口令 %s", DEFAULT_PASSWORD)
    finally:
        db.close()
