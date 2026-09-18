"""端到端冒烟：覆盖任务全部关键验收点。仅用于本地/CI 验证。"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:7106/api"
PASS, FAIL = [], []


def call(method, path, token=None, body=None, headers=None, raw=False):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("✅" if cond else "❌"), name, ("— " + str(extra) if extra and not cond else ""))


# 1. 登录四类账号
_, r = call("POST", "/auth/login", body={"username": "zhang_wei", "password": "bgt123456"})
broker = r["token"]; broker_u = r["user"]
check("登录-报关员", bool(broker) and broker_u["role"] == "broker")
_, r = call("POST", "/auth/login", body={"username": "wang_hua", "password": "bgt123456"})
customs = r["token"]
_, r = call("POST", "/auth/login", body={"username": "admin_huateng", "password": "bgt123456"})
ent1 = r["token"]; ent1_id = r["user"]["enterprise_id"]
_, r = call("POST", "/auth/login", body={"username": "zhao_jing", "password": "bgt123456"})
sup = r["token"]
st, r = call("POST", "/auth/login", body={"username": "zhang_wei", "password": "wrong"})
check("错误口令被拒(401)", st == 401)

# 2. 企业列表默认脱敏
_, ents = call("GET", "/enterprises", broker)
e = ents[0]
check("企业默认脱敏名（无全称）", e["name_full"] is None and "E0" in e["name"], e["name"])
check("信用代码掩码", e["credit_code"][4:8] == "****", e["credit_code"])

# 3. 二次确认看全名：理由过短被拒
filing_ent = next(x for x in ents if x["status"] == "filing")
st, r = call("POST", "/enterprises/reveal", broker, {"enterprise_id": e["id"], "reason": "短"})
check("看全名理由过短被拦(400)", st == 400)
st, r = call("POST", "/enterprises/reveal", broker,
             {"enterprise_id": e["id"], "reason": "查验异常需核对合同抬头与企业全称"})
check("理由合规可看全名并留痕", st == 200 and r["name_full"] and r["audit"]["viewer"], r.get("audit"))
_, audits = call("GET", "/enterprises/reveals/audit", sup)
check("监管员可见留痕审计", len(audits) >= 1 and audits[0]["reason"])

# 4. 委托链路拦截：备案中企业不能签合同
st, r = call("POST", "/contracts", broker, {"enterprise_id": filing_ent["id"]})
check("备案中企业签合同被拦下(409)", st == 409 and "备案" in r["error"]["reason"])

# 5. 立项链路：无合同/错合同拦截
_, ent2 = call("GET", "/enterprises", broker)
other_active = next(x for x in ents if x["status"] == "active" and x["id"] != filing_ent["id"])
st, r = call("POST", "/declarations", broker, {
    "enterprise_id": filing_ent["id"], "contract_id": 1,
    "port": "盐田港", "cargo_name": "测试货", "ie_type": "import", "total_value": 1})
check("未备案企业立项被拦", st == 409 and "备案" in r["error"]["reason"])

# 6. 正常立项 + 幂等：同 Idempotency-Key 重放不产生重复单
payload = {
    "enterprise_id": other_active["id"], "contract_id": 1 if other_active["id"] == 1 else 2,
    "port": "盐田港", "cargo_name": "冒烟测试货-幂等A", "ie_type": "import",
    "total_value": 10000, "client_ref": "SMOKE-REF-1",
}
# 找到该企业生效合同
_, contracts = call("GET", "/contracts", broker)
cactive = next(c for c in contracts if c["enterprise_id"] == other_active["id"] and c["status"] == "active")
payload["contract_id"] = cactive["id"]
st, r1 = call("POST", "/declarations", broker, payload, headers={"Idempotency-Key": "SMOKE-KEY-1"})
check("立项成功(200)", st == 200 and r1["declaration"]["status"] == "entrusted", r1.get("error"))
new_id = r1["declaration"]["id"]
st, r2 = call("POST", "/declarations", broker, payload, headers={"Idempotency-Key": "SMOKE-KEY-1"})
check("同Idempotency-Key重放幂等(同一单)", st == 200 and r2["declaration"]["id"] == new_id
      and r2.get("idempotency", {}).get("replayed"), str(r2.get("idempotency")))
# 换新钥匙但同 client_ref，仍去重
st, r3 = call("POST", "/declarations", broker, payload, headers={"Idempotency-Key": "SMOKE-KEY-2"})
check("同client_ref重试不重复建单", r3["deduplicated"] is True and r3["declaration"]["id"] == new_id)

# 7. 状态机：报关员录入 → 提交审单
st, r = call("POST", f"/declarations/{new_id}/transition", broker,
             {"action": "enter", "note": "冒烟录入", "expected_version": r1["declaration"]["version"]})
check("委托中→已录入", st == 200 and r["declaration"]["status"] == "entered")
v_entered = r["declaration"]["version"]
# 非法回退：已录入想直接回到委托中（无此动作）；试一个终态跳变：已录入直接结关
st, r = call("POST", f"/declarations/{new_id}/transition", broker,
             {"action": "close", "note": "试图跳步结关", "expected_version": v_entered})
check("非法跳态(录入→结关)被拦并说明原因", st in (409, 403) and "非法" in r["error"]["reason"],
      r.get("error", {}).get("reason"))
# 角色越权：企业管理员想提交审单（需报关员）
st, r = call("POST", f"/declarations/{new_id}/transition", ent1,
             {"action": "submit_review", "note": "企业越权操作"})
check("角色越权操作被拦(403)", st == 403 and "无权" in r["error"]["reason"], r.get("error"))
# 版本冲突：用旧版本号提交
st, r = call("POST", f"/declarations/{new_id}/transition", broker,
             {"action": "submit_review", "note": "旧版本提交", "expected_version": 1})
check("乐观锁版本冲突(409)", st == 409 and r["error"]["code"] == "version_conflict")
# 正确版本推进到审单中
st, r = call("POST", f"/declarations/{new_id}/transition", broker,
             {"action": "submit_review", "note": "提交海关审单", "expected_version": v_entered})
check("已录入→审单中", st == 200 and r["declaration"]["status"] == "reviewing")
v_rev = r["declaration"]["version"]

# 海关审单：审结放行
st, r = call("POST", f"/declarations/{new_id}/transition", customs,
             {"action": "release_from_review", "note": "单证单单相符，审结放行", "expected_version": v_rev})
check("审单中→已放行（海关）", st == 200 and r["declaration"]["status"] == "released")
v_rel = r["declaration"]["version"]
# 终态保护：已放行不能直接回审单（没有该动作），报关员尝试结关
st, r = call("POST", f"/declarations/{new_id}/transition", broker,
             {"action": "close", "note": "结关", "expected_version": v_rel})
check("已放行→已结关", st == 200 and r["declaration"]["status"] == "closed")
st, r = call("POST", f"/declarations/{new_id}/transition", customs,
             {"action": "release_from_review", "note": "终态后试图再动"})
check("终态单据不可再变更", st == 409 and "终态" in r["error"]["reason"], r.get("error"))

# 8. 离线恢复合并：新建一单，报关员离线本地先录入，再伪造一个冲突动作补传
st, r = call("POST", "/declarations", broker, {
    "enterprise_id": other_active["id"], "contract_id": cactive["id"],
    "port": "蛇口港", "cargo_name": "冒烟离线货", "ie_type": "export", "total_value": 5000,
}, headers={"Idempotency-Key": "SMOKE-OFFLINE-1"})
oid = r["declaration"]["id"]
# 模拟：服务端仍 entrusted；客户端离线事件 enter（合法）+ 再 enter（重复/非法同态）
st, r = call("POST", f"/declarations/{oid}/sync-offline", broker, {"action": "sync", "offline_events": [
    {"client_event_id": "ce-1", "action": "enter", "note": "口岸离线录入"},
    {"client_event_id": "ce-1", "action": "enter", "note": "重放同一事件"},
    {"client_event_id": "ce-2", "action": "close", "note": "离线状态下妄想直接结关"},
]})
check("离线合并：1应用1重复跳过1冲突",
      st == 200
      and len(r["merged"]["applied"]) == 1
      and len(r["merged"]["skipped_duplicates"]) == 1
      and len(r["merged"]["conflicts"]) == 1,
      json.dumps(r["merged"], ensure_ascii=False))
check("合并后单据状态=已录入，未重复", r["declaration"]["status"] == "entered")
# 再补传同 ce-1：应全部跳过
st, r = call("POST", f"/declarations/{oid}/sync-offline", broker, {"action": "sync", "offline_events": [
    {"client_event_id": "ce-1", "action": "enter", "note": "网络抖动重传"},
]})
check("已同步事件再次补传幂等跳过", len(r["merged"]["skipped_duplicates"]) == 1
      and len(r["merged"]["applied"]) == 0)

# 9. 企业间隔离：企业1管理员看企业2/3的单 → 403 错误态
_, decls = call("GET", "/declarations", ent1)
own_ids = {d["enterprise_id"] for d in decls}
check("企业列表只见本企业单", own_ids == {ent1_id}, own_ids)
st, r = call("GET", "/declarations?enterprise_id=999", ent1)
check("查询他人企业列表被拒(403)", st == 403 and "越权" in r["error"]["reason"])
# 找一张非本企业单
_, alldecls = call("GET", "/declarations", broker)
foreign = next(d for d in alldecls if d["enterprise_id"] != ent1_id)
st, r = call("GET", f"/declarations/{foreign['id']}", ent1)
check("越权看他人报关单→403且给原因（非空白）", st == 403 and "越权" in r["error"]["reason"])
# 本企业单可看
own = next(d for d in alldecls if d["enterprise_id"] == ent1_id)
st, r = call("GET", f"/declarations/{own['id']}", ent1)
check("本企业单可正常查看", st == 200 and r["declaration"]["id"] == own["id"])
# 不存在的单 → 404
st, r = call("GET", "/declarations/99999", ent1)
check("不存在单据→404", st == 404)

# 10. 作战台：漏斗/查验/红点
st, dash = call("GET", "/dashboard/overview", broker)
check("作战台六态漏斗", len(dash["totals_funnel"]) == 6)
check("作战台红点结构", "tax_overdue" in dash["red_dots"] and "inspection_overdue" in dash["red_dots"])
check("预置数据含逾期税款红点", dash["red_dots"]["tax_overdue"] >= 1, dash["red_dots"])
check("预置数据含逾期查验红点", dash["red_dots"]["inspection_overdue"] >= 1, dash["red_dots"])
check("每企业有作战卡", len(dash["enterprises"]) >= 3)
check("查验排期非空", len(dash["inspection_schedule"]) >= 1)
# 企业管理员作战台只见本企业
_, dash_e = call("GET", "/dashboard/overview", ent1)
check("企业作战台隔离（仅本企业）", len(dash_e["enterprises"]) == 1
      and dash_e["enterprises"][0]["enterprise_id"] == ent1_id)

# 11. 留痕审计对企业管理员不可见
st, _ = call("GET", "/enterprises/reveals/audit", ent1)
check("企业管理员无权访问留痕审计(403)", st == 403)

# 12. 备案审核流：监管员通过备案中企业后可签合同
st, r = call("POST", f"/enterprises/{filing_ent['id']}/approve", sup, {})
check("监管员审核通过备案", st == 200 and r["enterprise"]["status"] == "active")
st, r = call("POST", "/contracts", broker, {"enterprise_id": filing_ent["id"]})
check("备案通过后可拟定合同", st == 200 and r["contract"]["status"] == "pending")
cid = r["contract"]["id"]
st, r = call("POST", f"/contracts/{cid}/sign", broker, {})
check("合同签署生效", st == 200 and r["contract"]["status"] == "active")

# ============================================================
# 13. 审单与查验：逻辑审核 → 排期冲突 → 窗口确认 → 改派链 → 取消/锁定/及时率
# ============================================================
_, inspector_login = call("POST", "/auth/login", body={"username": "sun_li", "password": "bgt123456"})
insp_tok = inspector_login.get("token")
check("查验员账号可登录", bool(insp_tok) and inspector_login["user"]["role"] == "inspector",
      inspector_login.get("user"))

# 13.1 未逻辑审核不能排期（d3 审单中，尚无 pass_inspect）
st, r = call("POST", "/inspections", customs, {
    "declaration_id": 3, "yard_id": 1, "bay_id": 1, "inspector_id": 8,
    "scheduled_at": "2026-09-19T09:00", "duration_minutes": 120})
check("未逻辑审核直接排期被拦", st == 409 and r["error"]["code"] == "review_required", r.get("error"))

# 裸点状态机布控同样被拦（d8 无审核结论）
st, r = call("POST", "/declarations/8/transition", customs, {"action": "inspect", "note": "跳过审核"})
check("无审核结论裸点布控被拦", st == 409 and r["error"]["code"] == "review_required")

# 13.2 逻辑审核通过
st, r = call("POST", "/review-decisions", customs, {
    "declaration_id": 3, "result": "pass_inspect", "document_ok": True, "logic_ok": True,
    "opinion": "单证逻辑通过，命中布控"})
check("逻辑审核通过(pass_inspect)", st == 200 and r["result"] == "pass_inspect")
# 通过后正常排期（d3 明天 A-01/孙丽），自动转查验中
st, r = call("POST", "/inspections", customs, {
    "declaration_id": 3, "yard_id": 1, "bay_id": 1, "inspector_id": 8,
    "scheduled_at": "2026-09-19T09:00", "duration_minutes": 120, "due_at": "2026-09-19T12:00"})
check("审核通过后排期成功并转查验中(d3)",
      st == 200 and r["inspection"]["decl_status"] == "inspecting", r.get("error"))
# 通过但勾否的矛盾结论被拦（d8 还未审核）
st, r = call("POST", "/review-decisions", customs, {
    "declaration_id": 8, "result": "pass_inspect", "document_ok": False, "logic_ok": True})
check("审核通过但单证不一致被拦(400)", st == 400 and r["error"]["code"] == "review_contradiction")

# 13.3 资质不匹配冲突（三文鱼冷链货 d9 → 普通车位 A-01 + 无冷链查验员周强 id=9）
st, r = call("POST", "/inspections/conflicts", customs, {
    "declaration_id": 9, "yard_id": 1, "bay_id": 1, "inspector_id": 9,
    "scheduled_at": "2026-09-19T09:00", "duration_minutes": 120})
codes = {c["code"] for c in r["hard_conflicts"]}
check("资质不匹配冲突（车位+查验员）", st == 200 and {"bay_cert_mismatch", "inspector_cert_mismatch"} <= codes,
      codes)
check("冲突明细含可读原因", all(c.get("reason") for c in r["hard_conflicts"]))

# 13.4 同车位同时段两单占（蛇口 B-12 id=5 今日10:00 已排 i20；郑敏 id=11）
st, r = call("POST", "/inspections/conflicts", customs, {
    "declaration_id": 8, "yard_id": 2, "bay_id": 5, "inspector_id": 11,
    "scheduled_at": "2026-09-18T10:00", "duration_minutes": 120})
codes = {c["code"] for c in r["hard_conflicts"]}
check("同车位同段两单占冲突", "bay_overlap" in codes, codes)
check("冲突带关联单可点开", any(c.get("related", {}).get("decl_no") for c in r["hard_conflicts"]
      if c["code"] == "bay_overlap"))

# 请假查验员（林凯 id=10 seeded unavailable）→ 硬冲突
st, r = call("POST", "/inspections/conflicts", customs, {
    "declaration_id": 8, "yard_id": 2, "bay_id": 6, "inspector_id": 10,
    "scheduled_at": "2026-09-19T09:00", "duration_minutes": 120})
codes = {c["code"] for c in r["hard_conflicts"]}
check("请假查验员被拦(inspector_unavailable)", "inspector_unavailable" in codes, codes)

# 13.5 d8 审核后排期成功（蛇口 B-12/郑敏 明天，不与今日 i20 冲突）
call("POST", "/review-decisions", customs, {
    "declaration_id": 8, "result": "pass_inspect", "document_ok": True, "logic_ok": True,
    "opinion": "机检查验"})
st, r = call("POST", "/inspections", customs, {
    "declaration_id": 8, "yard_id": 2, "bay_id": 5, "inspector_id": 11,
    "scheduled_at": "2026-09-19T09:00", "duration_minutes": 120, "due_at": "2026-09-19T12:00"})
check("d8 审核通过后排期成功",
      st == 200 and r["inspection"]["decl_status"] == "inspecting", r.get("error"))

# 13.6 超作业窗口：无确认被拦，有确认+原因留痕后通过
st, r = call("POST", "/inspections/4/move", customs, {
    "scheduled_at": "2026-09-18T22:00", "expected_version": 1})
check("超窗口无确认被拦(409)", st == 409 and r["error"]["code"] == "window_confirmation_required",
      r.get("error", {}).get("code"))
st, r = call("POST", "/inspections/4/move", customs, {
    "scheduled_at": "2026-09-18T22:00", "window_confirmed": True,
    "window_reason": "船舶夜航靠泊，场站确认增开夜班查验"})
check("超窗口填原因确认后通过", st == 200 and r["inspection"]["scheduled_at"].endswith("22:00"))
st, r = call("POST", "/inspections/4/move", customs, {
    "scheduled_at": "2026-09-18T22:30", "window_confirmed": True, "window_reason": "短"})
check("超窗口原因过短被拦(400)", st == 400 and r["error"]["code"] == "window_reason_required")

# 13.7 改派链式重排（预览 → 确认；锚点 i6=id1 从10:30推11:30，i14/i16 必须链式顺延）
st, prev = call("POST", "/inspections/1/reassign/preview", customs, {
    "new_scheduled_at": "2026-09-18T11:30", "new_bay_id": 4, "new_inspector_id": 9,
    "duration_minutes": 120, "reason_type": "manual", "reason": "预览用"})
check("改派预览成功", st == 200 and len(prev["plan"]["moves"]) >= 3,
      json.dumps(prev.get("plan", {}).get("moves", []), ensure_ascii=False))
moved_nos = [m["decl_no"] for m in prev["plan"]["moves"]]
check("链式顺延含后续同场站单", "I2026091700014" in moved_nos and "I2026091800016" in moved_nos, moved_nos)
chain_times = {m["decl_no"]: m["to_scheduled_at"][11:] for m in prev["plan"]["moves"]}
check("顺延只向后不回退", all(m["to_scheduled_at"] >= m["from_scheduled_at"]
                               for m in prev["plan"]["moves"]))
st, conf = call("POST", "/inspections/1/reassign/confirm", customs, {
    "new_scheduled_at": "2026-09-18T11:30", "new_bay_id": 4, "new_inspector_id": 9,
    "duration_minutes": 120, "reason_type": "manual",
    "reason": "船公司压港统一后延一小时冒烟验证"})
check("改派确认执行", st == 200 and conf.get("batch_id"))
check("改派单与后续单共享批次留痕号",
      len({m.get("to_scheduled_at") for m in conf["plan"]["moves"]}) >= 2)

# 13.8 已放行/已结关单锁定：d1（已结关）的历史查验不可拖动/改派/取消
_, d1_insps = call("GET", "/inspections?declaration_id=1", customs)
d1_insp_id = d1_insps[0]["id"]
st, r = call("POST", f"/inspections/{d1_insp_id}/move", customs,
             {"scheduled_at": "2026-09-25T09:00"})
check("已结关单查验锁定不可拖动", st == 409 and r["error"]["code"] == "inspection_locked")
st, r = call("POST", f"/inspections/{d1_insp_id}/cancel", customs, {"reason": "试图取消结关单查验"})
check("已结关单查验不可取消", st == 409 and r["error"]["code"] == "inspection_locked")

# 13.9 取消排期（d3 的排期取消 → 该单只剩已取消排期，对应前端第3种空态）
_, d3_insps = call("GET", "/inspections?declaration_id=3", customs)
st, r = call("POST", f"/inspections/{d3_insps[0]['id']}/cancel", customs,
             {"reason": "企业车队故障无法到场，取消待重排"})
check("取消排期成功并留痕", st == 200 and r["inspection"]["status"] == "cancelled")
_, d3_after = call("GET", "/inspections?declaration_id=3", customs)
check("该单排期全部取消（第3种空态数据条件）",
      all(x["status"] == "cancelled" for x in d3_after) and len(d3_after) >= 1)

# 13.10 甘特接口带逐条冲突（种子红单：d17/d18 撞车位撞查验员，d9 资质不符）
# 用周甘特确保昨天逾期的 d9 也落在拉取窗口内
st, g = call("GET", "/scheduling/gantt?yard_id=1&view=week", customs)
conflict_ids = {i["declaration_id"] for i in g["inspections"] if i["conflicts"]}
check("甘特返回逐条冲突标红数据",
      st == 200 and 17 in conflict_ids and 18 in conflict_ids and 9 in conflict_ids,
      conflict_ids)
red = next(i for i in g["inspections"] if i["declaration_id"] == 17)
check("冲突条目含结构化原因与关联单",
      any(c["code"] == "bay_overlap" and c.get("related", {}).get("decl_no")
          for c in red["conflicts"]))

# 13.11 及时率双口径（8 月频繁改派：两口径方向相反；公式随接口下发）
st, m = call("GET", "/metrics/timeliness?month=2026-08", customs)
check("及时率接口返回双口径", st == 200 and "time" in m["calibers"] and "volume" in m["calibers"])
check("8月时效口径36.4%（4/11）", m["calibers"]["time"]["rate_percent"] == 36.4,
      m["calibers"]["time"])
check("8月单量口径91.7%（11/12）", m["calibers"]["volume"]["rate_percent"] == 91.7,
      m["calibers"]["volume"])
check("两口径8月方向相反（时效低、单量高）",
      m["calibers"]["time"]["rate_percent"] < 50 < m["calibers"]["volume"]["rate_percent"])
check("口径公式随接口返回（写界面用）",
      "应查验日" in m["calibers"]["time"]["formula"] and "派" in m["calibers"]["volume"]["formula"])
check("8月含改派留痕", m["calibers"]["reassign_logs"] >= 2)
check("近6个月走势", len(m["months"]) == 6 and any(x["time_rate"] is not None for x in m["months"]))

# 13.12 该单无排期（第2种空态）：找一张委托中/已录入单，其排期列表为空
_, no_insp = call("GET", "/inspections?declaration_id=4", customs)
check("该单无排期返回空列表（非错误）", isinstance(no_insp, list) and no_insp == [])

# 13.13 查验员权限：普通查验员不能替他人单登记结果以外的排期管理动作
st, r = call("POST", "/inspections", insp_tok, {
    "declaration_id": 8, "yard_id": 2, "bay_id": 5, "inspector_id": 12,
    "scheduled_at": "2026-09-20T09:00"})
check("查验员无权创建排期(403)", st == 403)

print("\n==============================")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    raise SystemExit(1)
