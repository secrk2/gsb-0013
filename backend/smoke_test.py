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

print("\n==============================")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    raise SystemExit(1)
