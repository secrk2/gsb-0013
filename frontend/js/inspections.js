/* 查验排期模块：逻辑审核 → 双甘特拖拽 → 冲突逐条标红 → 改派链式重排 → 双口径及时率。
 *
 * 三种空/错态严格分开，绝不统一成「暂无数据」：
 *  1) 加载失败（网络/5xx）→ 错误态：错误码+原因+重试；
 *  2) 该单无排期（200 且空）→ 「该单暂无查验排期」空态 + 安排入口；
 *  3) 排期全部取消 → 「N 次排期均已取消」态 + 取消原因 + 重新安排入口。
 */
(function () {
  'use strict';
  const U = window.BGT;
  const { el, esc, modal, toast, fmtDate } = U;

  const DAY_SLOT_W = 36;   // 日视图每 30 分钟像素宽
  const WEEK_SLOT_W = 13;  // 周视图每 30 分钟像素宽
  const DAY_START = 7, DAY_END = 24;       // 日甘特显示 07:00–24:00（含夜班超窗口排期）
  const WEEK_START = 8, WEEK_END = 20;     // 周甘特每天显示 08:00–20:00
  const SLOT_MIN = 30;
  const LANE_W = 168;     // 左侧泳道标题列宽

  let state = {
    yardId: null, view: 'day', date: null, lane: 'bay', data: null,
    loading: false, error: null,
  };

  const pad = n => String(n).padStart(2, '0');
  const dstr = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const mondayOf = d => { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - x.getDay() + 1); return x; };
  const parseLocal = s => new Date(s); // 后端返回 naive UTC，当本地时间解析即可对齐演示
  const snap30 = d => { const x = new Date(d); x.setSeconds(0, 0); const m = x.getMinutes(); x.setMinutes(m < 15 ? 0 : m < 45 ? 30 : 60); return x; };
  const toLocalInput = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

  /* ================= 路由 ================= */

  U.route('/inspections', async (rest) => {
    const view = shell2();
    // #/inspections?yard=1&view=week&date=2026-09-18&inspection=5
    const params = new URLSearchParams((location.hash.split('?')[1] || ''));
    state.yardId = Number(params.get('yard')) || state.yardId;
    if (params.get('view')) state.view = params.get('view') === 'week' ? 'week' : 'day';
    state.date = params.get('date');
    state.error = null; state.data = null;
    await renderPage(view);
    const focusId = params.get('inspection');
    if (focusId) {
      const card = view.querySelector(`[data-insp-card="${focusId}"]`);
      if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 1600); }
    }
  });

  function shell2() { return U.shell('/inspections'); }

  function navQuery(extra = {}) {
    const p = new URLSearchParams();
    if (state.yardId) p.set('yard', state.yardId);
    p.set('view', state.view);
    if (state.date) p.set('date', state.date);
    Object.entries(extra).forEach(([k, v]) => v != null && p.set(k, v));
    return `#/inspections?${p.toString()}`;
  }
  function gotoNav(extra) { location.hash = navQuery(extra); }

  /* ================= 页面骨架 ================= */

  async function renderPage(view) {
    view.innerHTML = U.loading();
    let yards = [];
    try {
      yards = await Api.get('/yards');
    } catch (e) {
      view.innerHTML = '';
      view.appendChild(errorState(e, '场站资源加载失败，甘特图无法初始化', () => U.reroute()));
      return;
    }
    if (!yards.length) {
      view.innerHTML = '';
      view.appendChild(el(`<div class="empty-state"><div class="ico">🏗️</div>
        <h3>尚未配置任何监管场站</h3><div class="muted small">请先在场站基础数据中维护监管场站、查验车位与查验员。</div></div>`));
      return;
    }
    if (!state.yardId || !yards.some(y => y.id === state.yardId)) state.yardId = yards[0].id;

    view.innerHTML = pageHtml(yards);
    bindChrome(view, yards);
    await loadGantt(view);
  }

  function pageHtml(yards) {
    const today = dstr(new Date());
    const anchor = state.date || today;
    return `
      <div class="page-head">
        <div><h2>🗓️ 查验排期甘特</h2>
          <div class="desc">逻辑审核通过后安排查验；拖拽改期，冲突逐条标红可点开原因；车故障/人请假走改派链式重排。</div></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" id="btn-metrics">📊 查验及时率（双口径）</button>
          <button class="btn solid" id="btn-refresh">🔄 刷新</button>
        </div>
      </div>
      <div class="gantt-toolbar card">
        <div class="fg"><label>监管场站</label>
          <select id="g-yard">${yards.map(y => `<option value="${y.id}" ${y.id === state.yardId ? 'selected' : ''}>${esc(y.name)}（${esc(y.port)}）</option>`).join('')}</select></div>
        <div class="seg-btns" id="g-view">
          <button data-v="day" class="${state.view === 'day' ? 'on' : ''}">📅 日甘特</button>
          <button data-v="week" class="${state.view === 'week' ? 'on' : ''}">🗓️ 周甘特</button>
        </div>
        <div class="seg-btns" id="g-lane">
          <button data-l="bay" class="${state.lane === 'bay' ? 'on' : ''}">按车位泳道</button>
          <button data-l="inspector" class="${state.lane === 'inspector' ? 'on' : ''}">按查验员泳道</button>
        </div>
        <div class="g-nav">
          <button class="btn sm" id="g-prev">◀ ${state.view === 'week' ? '上周' : '前一天'}</button>
          <button class="btn sm" id="g-today">今天</button>
          <button class="btn sm" id="g-next">${state.view === 'week' ? '下周' : '后一天'} ▶</button>
          <span class="g-anchor" id="g-anchor">${anchor}${state.view === 'week' ? ' 所在周' : ''}</span>
        </div>
      </div>
      <div id="gantt-body"></div>`;
  }

  function bindChrome(view, yards) {
    view.querySelector('#g-yard').onchange = e => { state.yardId = Number(e.target.value); gotoNav(); };
    view.querySelectorAll('#g-view button').forEach(b => b.onclick = () => { state.view = b.dataset.v; gotoNav(); });
    view.querySelectorAll('#g-lane button').forEach(b => b.onclick = () => { state.lane = b.dataset.l; renderGantt(view); });
    view.querySelector('#g-prev').onclick = () => shiftDate(state.view === 'week' ? -7 : -1);
    view.querySelector('#g-next').onclick = () => shiftDate(state.view === 'week' ? 7 : 1);
    view.querySelector('#g-today').onclick = () => { state.date = null; gotoNav(); };
    view.querySelector('#btn-refresh').onclick = () => U.reroute();
    view.querySelector('#btn-metrics').onclick = openTimeliness;
  }

  function shiftDate(delta) {
    const base = state.date ? new Date(state.date + 'T00:00:00') : new Date();
    base.setDate(base.getDate() + delta);
    state.date = dstr(base);
    gotoNav();
  }

  async function loadGantt(view) {
    const body = view.querySelector('#gantt-body');
    body.innerHTML = U.loading();
    const qs = new URLSearchParams({ yard_id: state.yardId, view: state.view });
    if (state.date) qs.set('date', state.date);
    try {
      state.data = await Api.get('/scheduling/gantt?' + qs.toString());
      state.error = null;
    } catch (e) {
      state.error = e;
      body.innerHTML = '';
      body.appendChild(errorState(e, '甘特排期加载失败', () => U.reroute()));
      return;
    }
    const anchor = view.querySelector('#g-anchor');
    if (anchor) anchor.textContent = state.data.anchor_date + (state.view === 'week' ? ' 所在周' : '');
    renderGantt(view);
  }

  /* ================= 甘特渲染 ================= */

  function viewWindows() {
    // 返回 [{date, label, lo:Date(07:00), hi:Date(21:00)}]
    const todayStr = dstr(new Date());
    const a = state.date ? new Date(state.date + 'T00:00:00') : (() => { const x = new Date(); x.setHours(0, 0, 0, 0); return x; })();
    if (state.view === 'week') {
      const mon = mondayOf(a);
      const names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
      return Array.from({ length: 7 }, (_, i) => {
        const d = new Date(mon); d.setDate(mon.getDate() + i);
        const ds = dstr(d);
        return { date: ds, label: names[i] + ' ' + ds.slice(5) + (ds === todayStr ? '（今天）' : ''),
                 lo: at(d, WEEK_START), hi: at(d, WEEK_END), dow: d.getDay() };
      });
    }
    const ds = dstr(a);
    return [{ date: ds, label: ds + (ds === todayStr ? '（今天）' : ''),
             lo: at(a, DAY_START), hi: at(a, DAY_END), dow: a.getDay() }];
  }
  function at(d, h) { const x = new Date(d); x.setHours(h, 0, 0, 0); return x; }

  function renderGantt(view) {
    const body = view.querySelector('#gantt-body');
    const { yard, inspections } = state.data;
    const wins = viewWindows();
    const slotW = state.view === 'day' ? DAY_SLOT_W : WEEK_SLOT_W;
    const hs = state.view === 'day' ? DAY_START : WEEK_START;
    const he = state.view === 'day' ? DAY_END : WEEK_END;
    const slotsPerDay = (he - hs) * 2;
    const dayW = slotsPerDay * slotW;
    const totalW = wins.length * dayW;
    const now = parseLocal(state.data.now);

    // 泳道：车位 or 查验员
    const canManage = ['customs', 'supervisor'].includes(Api.user.role);
    const lanes = state.lane === 'bay'
      ? yard.bays.map(b => ({ key: 'bay-' + b.id, id: b.id, name: b.name, sub: b.cert_tags.map(certLabel).join(' / '),
                             disabled: b.out_of_service, disabledReason: b.out_of_service_reason, raw: b }))
      : yard.inspectors.map(u => ({ key: 'insp-' + u.id, id: u.id, name: u.display_name.replace(/（.*?）/g, ''),
                                   sub: u.certs.map(certLabel).join(' / '),
                                   disabled: !u.available, disabledReason: u.unavailable_reason, raw: u }));

    body.innerHTML = `
      ${conflictSummary(inspections)}
      <div class="gantt-scroll">
        <div class="gantt" style="--slot-w:${slotW}px;--lane-w:${LANE_W}px">
          <div class="g-head-row">
            <div class="g-lane g-corner">${state.lane === 'bay' ? '查验车位 ＼ 时间' : '查验员 ＼ 时间'}</div>
            <div class="g-head-track" style="width:${totalW}px">
              ${wins.map(w => `<div class="g-day-head" style="width:${dayW}px">
                <div class="dh-label ${w.dow === 0 || w.dow === 6 ? 'weekend' : ''}">${esc(w.label)}</div>
                <div class="dh-hours">${hourTicks(hs, he, slotW, now, w)}</div>
              </div>`).join('')}
            </div>
          </div>
          ${lanes.map(lane => `
            <div class="g-row ${lane.disabled ? 'lane-disabled' : ''}" data-lane-key="${lane.key}" data-lane-id="${lane.id}">
              <div class="g-lane">
                <div class="ln-name">${esc(lane.name)}${lane.disabled ? ' <span class="dot red">停用</span>' : ''}</div>
                <div class="ln-sub">${esc(lane.sub || '—')}</div>
                ${lane.disabledReason ? `<div class="ln-reason">${esc(lane.disabledReason)}</div>` : ''}
              </div>
              <div class="g-track" style="width:${totalW}px">
                ${wins.map((w, wi) => `<div class="g-day" data-day-idx="${wi}" data-day-date="${w.date}" style="width:${dayW}px">
                  ${gridLines(hs, he, slotW)}
                  ${nowMarker(now, w, dayW, hs, slotW)}
                </div>`).join('')}
              </div>
            </div>`).join('')}
        </div>
      </div>
      <div class="gantt-legend">
        <span><i class="lg st-scheduled"></i>待查验</span>
        <span><i class="lg st-inspecting"></i>查验中</span>
        <span><i class="lg st-done"></i>已完成</span>
        <span><i class="lg st-cancelled"></i>已取消</span>
        <span><i class="lg conflict"></i>存在冲突（点开看原因）</span>
        <span><i class="lg locked"></i>已放行/结关锁定</span>
        <span class="muted small">拖拽卡片改期/换资源；落点超作业时段需二次确认填原因；拖到其他泳道即换车位/查验员。</span>
      </div>`;

    // 卡片按泳道挂载到对应 track（绝对定位，纵向天然对齐到该行）
    const rowByLane = {};
    body.querySelectorAll('.g-row').forEach(r => { rowByLane[r.dataset.laneKey] = r; });
    const stacked = {}; // 同一泳道内重叠卡片做轻微纵向错位
    for (const ins of inspections) {
      const s = parseLocal(ins.scheduled_at), e = parseLocal(ins.scheduled_end);
      const laneKey = state.lane === 'bay' ? ('bay-' + (ins.bay_id ?? 'none')) : ('insp-' + (ins.inspector_id ?? 'none'));
      const row = rowByLane[laneKey];
      if (!row) continue;
      const track = row.querySelector('.g-track');
      wins.forEach((w, wi) => {
        const vs = Math.max(s, w.lo), ve = Math.min(e, w.hi);
        if (vs >= ve) return;
        const left = wi * dayW + ((vs - w.lo) / 60000 / SLOT_MIN) * slotW;
        const width = Math.max(24, ((ve - vs) / 60000 / SLOT_MIN) * slotW - 4);
        const conflicted = ins.conflicts && ins.conflicts.length;
        const stackKey = laneKey + ':' + wi;
        const stackN = (stacked[stackKey] = (stacked[stackKey] || 0) + 1);
        const top = 2 + ((stackN - 1) % 2) * 16;
        const cls = ['g-card', 'st-' + ins.status, ins.locked ? 'locked' : '', conflicted ? 'conflict' : '']
          .filter(Boolean).join(' ');
        const card = el(`
          <div class="${cls}" data-insp-id="${ins.id}" data-insp-card="${ins.id}"
               draggable="${(!ins.locked && !['done', 'cancelled'].includes(ins.status) && canManage).toString()}"
               style="left:${left}px;width:${width}px;top:${top}px"
               title="${esc(ins.decl_no)} ${fmtDate(ins.scheduled_at)}">
            ${conflicted ? '<span class="g-warn">⚠</span>' : ''}
            <span class="g-no">${esc(ins.decl_no)}</span>
            <span class="g-cargo">${esc(ins.cargo_name)}</span>
            <span class="g-time">${pad(vs.getHours())}:${pad(vs.getMinutes())}–${pad(ve.getHours())}:${pad(ve.getMinutes())}</span>
          </div>`);
        track.appendChild(card);
      });
    }

    bindGanttEvents(view, wins, slotW, hs, dayW);
  }

  function nowMarker(now, w, dayW, hs, slotW) {
    if (now < w.lo || now >= w.hi) return '';
    const left = ((now - w.lo) / 60000 / SLOT_MIN) * slotW;
    return `<div class="now-marker" style="left:${left}px" title="当前时间"></div>`;
  }

  function gridLines(hs, he, slotW) {
    let html = '';
    for (let h = hs; h <= he; h++) {
      html += `<div class="gl ${h % 2 === 0 ? 'solid' : ''}" style="left:${(h - hs) * 2 * slotW}px"></div>`;
    }
    return html;
  }
  function hourTicks(hs, he, slotW, now, w) {
    const arr = [];
    for (let h = hs; h < he; h++) arr.push(`<span style="left:${(h - hs) * 2 * slotW}px">${pad(h)}</span>`);
    return arr.join('');
  }

  function conflictSummary(inspections) {
    const bad = inspections.filter(i => i.conflicts && i.conflicts.length);
    if (!bad.length) return '';
    const n = bad.reduce((s, i) => s + i.conflicts.length, 0);
    return `<div class="conflict-banner">
      <b>⚠ 检测到 ${bad.length} 单、${n} 条排期冲突</b>
      <span class="muted small">红色卡片可点开查看逐条原因；拖走冲突单或用「改派」链式重排解除。</span>
    </div>`;
  }

  function certLabel(c) {
    return ({ normal: '普通', cold: '冷链', food: '食品', heavy: '重机', danger: '危化' })[c] || c;
  }

  /* ================= 拖拽 ================= */

  function bindGanttEvents(view, wins, slotW, hs, dayW) {
    let dragId = null;
    view.querySelectorAll('.g-card').forEach(card => {
      card.addEventListener('dragstart', e => {
        dragId = Number(card.dataset.inspId);
        card.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(dragId));
      });
      card.addEventListener('dragend', () => { card.classList.remove('dragging'); dragId = null; });
      card.addEventListener('click', () => openInspection(Number(card.dataset.inspId)));
    });
    view.querySelectorAll('.g-row').forEach(row => {
      row.addEventListener('dragover', e => {
        if (dragId == null) return;
        e.preventDefault();
        row.classList.add('drop-hint');
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop-hint'));
      row.addEventListener('drop', async e => {
        e.preventDefault();
        row.classList.remove('drop-hint');
        if (dragId == null) return;
        const insp = state.data.inspections.find(x => x.id === dragId);
        if (!insp) return;
        const rect = row.querySelector('.g-track').getBoundingClientRect();
        const x = Math.max(0, e.clientX - rect.left);
        const dayIdx = Math.min(wins.length - 1, Math.floor(x / dayW));
        const inDayX = x - dayIdx * dayW;
        // 30 分钟吸附：1 个 slotW = 30 分钟
        const minutesFromHs = Math.round(inDayX / slotW) * SLOT_MIN;
        const target = new Date(wins[dayIdx].lo.getTime() + minutesFromHs * 60000);
        const laneKey = row.dataset.laneKey;
        const laneId = Number(row.dataset.laneId);
        await handleDrop(insp, target, laneKey, laneId);
      });
    });
  }

  async function handleDrop(insp, start, laneKey, laneId) {
    const durationMin = Math.max(30, Math.round((parseLocal(insp.scheduled_end) - parseLocal(insp.scheduled_at)) / 60000));
    const end = new Date(start); end.setMinutes(end.getMinutes() + durationMin);
    const isBayLane = laneKey.startsWith('bay-');
    const payload = {
      scheduled_at: toLocalInput(start),
      scheduled_end: toLocalInput(end),
      expected_version: insp.version,
    };
    if (isBayLane) payload.bay_id = laneId;
    else payload.inspector_id = laneId;

    // 预检：实时调冲突接口
    const checkBody = {
      declaration_id: insp.declaration_id, yard_id: state.data.yard.id,
      bay_id: isBayLane ? laneId : insp.bay_id,
      inspector_id: isBayLane ? insp.inspector_id : laneId,
      scheduled_at: toLocalInput(start),
      duration_minutes: durationMin,
    };
    let pre;
    try {
      pre = await Api.post('/inspections/conflicts', checkBody);
    } catch (e) {
      toast(e.reason || '冲突预检失败', 'error'); return;
    }

    if (pre.hard_conflicts && pre.hard_conflicts.length) {
      openConflictModal('拖拽落点存在冲突，排期未移动', pre.hard_conflicts, insp);
      return;
    }
    if (pre.window_warnings && pre.window_warnings.length) {
      const wr = await confirmWindow(pre.window_warnings, `将「${insp.decl_no}」拖到 ${fmtDate(toLocalInput(start))}`);
      if (!wr) { U.reroute(); return; }
      payload.window_confirmed = true; payload.window_reason = wr.reason;
    }
    try {
      await Api.post(`/inspections/${insp.id}/move`, payload);
      toast('排期已更新', 'success');
      U.reroute();
    } catch (e) {
      if (e.code === 'version_conflict') {
        toast('排期已被他人修改（版本冲突），已为你刷新最新甘特', 'warn'); U.reroute();
      } else if (e.code === 'schedule_conflict') {
        openConflictModal(e.reason, e.data?.error?.details || [], insp);
      } else toast(e.reason, 'error');
    }
  }

  /* ================= 冲突弹窗（逐条标红、可点开原因） ================= */

  function openConflictModal(title, conflicts, insp) {
    const items = (conflicts || []).map((c, i) => `
      <div class="conf-item-card">
        <div class="ci-head">
          <span class="ci-badge">冲突 ${i + 1}</span>
          <b>${esc(c.title || c.code)}</b>
        </div>
        <div class="ci-reason">${esc(c.reason)}</div>
        ${c.related ? `<div class="ci-related" data-decl="${c.related.declaration_id}">
          🔗 冲突关联单：<span class="mono">${esc(c.related.decl_no)}</span>
          （${esc(c.related.cargo_name || '')}，${esc((c.related.scheduled_at || '').replace('T', ' '))}）<span class="go">点开查看 ▸</span></div>` : ''}
      </div>`).join('');
    const m = modal({
      title: '🚫 ' + title, wide: true,
      body: `<div class="danger-box">以下为逐条冲突原因，系统未做任何排期变更。请调整落点，或使用「改派」触发链式重排。</div>
             <div class="conf-list">${items || '<div class="muted">无结构化冲突明细</div>'}</div>`,
      footer: `<button class="btn" id="cc-refresh">刷新甘特</button>
               <button class="btn solid" id="cc-reassign">对该单走改派链</button>`,
    });
    m.mask.querySelectorAll('[data-decl]').forEach(x => {
      x.onclick = () => { m.close(); location.hash = `#/declarations/${x.dataset.decl}`; };
    });
    m.mask.querySelector('#cc-refresh').onclick = () => { m.close(); U.reroute(); };
    m.mask.querySelector('#cc-reassign').onclick = () => { m.close(); openReassign(insp); };
  }

  /* ================= 超窗口二次确认 ================= */

  function confirmWindow(warnings, ctxText) {
    return new Promise(resolve => {
      const m = modal({
        title: '⚠ 落点超出排期窗口 · 二次确认',
        body: `
          <div class="warn-box">${esc(ctxText)}，该落点不在常规排期窗口内：</div>
          <ul class="window-list">${warnings.map(w => `<li><b>${esc(w.title)}</b>：${esc(w.reason)}</li>`).join('')}</ul>
          <label>超窗口排期原因（必填，≥5 字，将写入留痕供监管审计）</label>
          <textarea id="win-reason" rows="3" placeholder="如：船公司压港夜间到场，经场站确认增开夜班查验"></textarea>`,
        footer: `<button class="btn" id="win-cancel">取消拖拽</button>
                 <button class="btn gold" id="win-ok">确认超窗口排期</button>`,
      });
      m.mask.querySelector('#win-cancel').onclick = () => { m.close(); resolve(null); };
      m.mask.querySelector('#win-ok').onclick = () => {
        const reason = m.mask.querySelector('#win-reason').value.trim();
        if (reason.length < 5) { toast('原因不少于 5 个字', 'warn'); return; }
        m.close(); resolve({ confirmed: true, reason });
      };
    });
  }

  /* ================= 条详情 / 取消 / 登记结果 ================= */

  async function openInspection(id) {
    let detail;
    try { detail = await Api.get(`/inspections/${id}`); }
    catch (e) { toast(e.reason, 'error'); return; }
    const i = detail.inspection;
    const role = Api.user.role;
    const canManage = ['customs', 'supervisor'].includes(role);
    const canDo = canManage || (role === 'inspector');
    const conflictHtml = (i.conflicts || []).length
      ? `<div class="conf-brief">${(i.conflicts || []).map((c, k) =>
          `<div class="cb-row"><span class="ci-badge">冲突 ${k + 1}</span><b>${esc(c.title)}</b><div class="ci-reason">${esc(c.reason)}</div>
           ${c.related ? `<div class="ci-related" data-decl="${c.related.declaration_id}">🔗 ${esc(c.related.decl_no)} <span class="go">查看 ▸</span></div>` : ''}</div>`).join('')}</div>`
      : '';
    const m = modal({
      title: `🔎 ${i.decl_no} · 查验排期`, wide: true,
      body: `
        ${i.locked ? '<div class="danger-box">该报关单已放行/已结关，排期已锁定，仅可查看。</div>' : ''}
        <div class="insp-grid">
          <div><label>状态</label><div>${statusPill(i)}</div></div>
          <div><label>计划时段</label><div>${fmtDate(i.scheduled_at)} – ${(i.scheduled_end || '').slice(11)}</div></div>
          <div><label>应查验日</label><div>${fmtDate(i.due_at)}</div></div>
          <div><label>实际完成</label><div>${i.finished_at ? fmtDate(i.finished_at) : '—'}</div></div>
          <div><label>场站</label><div>${esc(i.yard_name || '—')}</div></div>
          <div><label>车位</label><div>${esc(i.bay || '—')} <span class="muted small">${(i.bay_cert_tags || []).map(certLabel).join('/')}</span></div></div>
          <div><label>查验员</label><div>${esc(i.inspector_name || '未指派')}</div></div>
          <div><label>货物资质</label><div>${(i.required_certs || []).map(certLabel).join(' / ')}</div></div>
          <div class="full"><label>结果备注</label><div>${esc(i.result_note || '—')}</div></div>
        </div>
        ${conflictHtml}
        <h4 class="log-title">🧾 排期留痕（${detail.logs.length}）</h4>
        <div class="mini-timeline">
          ${detail.logs.slice().reverse().map(lg => `
            <div class="mtl-item">
              <div><span class="mtl-act">${esc(lg.action_label)}</span>
                ${lg.reassign_batch_id ? `<span class="muted small mono">批次 ${esc(lg.reassign_batch_id)}</span>` : ''}</div>
              <div class="muted small">${esc(lg.actor_name)} · ${fmtDate(lg.created_at)}${lg.reason ? ' · ' + esc(lg.reason) : ''}</div>
            </div>`).join('') || '<div class="muted small">暂无留痕</div>'}
        </div>`,
      footer: [
        i.status === 'scheduled' && canDo ? '<button class="btn" id="i-start">▶ 开始查验</button>' : '',
        ['scheduled', 'inspecting'].includes(i.status) && canDo ? '<button class="btn solid" id="i-finish">登记查验结果</button>' : '',
        canManage && !i.locked && ['scheduled', 'inspecting'].includes(i.status) ? '<button class="btn gold" id="i-reassign">🔀 改派（车故障/人请假链式重排）</button>' : '',
        canManage && !i.locked && ['scheduled', 'inspecting'].includes(i.status) ? '<button class="btn danger" id="i-cancel">取消排期</button>' : '',
        '<button class="btn" id="i-decl">查看报关单</button>',
      ].filter(Boolean).join(''),
    });
    m.mask.querySelectorAll('.ci-related').forEach(x => x.onclick = () => { m.close(); location.hash = `#/declarations/${x.dataset.decl}`; });
    m.mask.querySelector('#i-decl').onclick = () => { m.close(); location.hash = `#/declarations/${i.declaration_id}`; };
    const st = m.mask.querySelector('#i-start');
    if (st) st.onclick = async () => {
      try { await Api.post(`/inspections/${id}/start`, {}); toast('已开始查验', 'success'); m.close(); U.reroute(); }
      catch (e) { toast(e.reason, 'error'); }
    };
    const fn = m.mask.querySelector('#i-finish');
    if (fn) fn.onclick = () => { m.close(); openFinish(id); };
    const rs = m.mask.querySelector('#i-reassign');
    if (rs) rs.onclick = () => { m.close(); openReassign(i); };
    const cn = m.mask.querySelector('#i-cancel');
    if (cn) cn.onclick = async () => {
      const reason = await U.confirmNote({
        title: '取消查验排期', danger: true, confirmText: '确认取消',
        message: `确认取消 ${i.decl_no} 的查验排期？取消后释放车位与查验员，需重新排期。`,
        placeholder: '请填写取消原因（不少于5字，如：企业车辆故障无法到场/场站检修）',
      });
      if (!reason) return;
      if (reason.length < 5) { toast('取消原因不少于 5 个字（需留痕审计）', 'warn'); return; }
      try { await Api.post(`/inspections/${id}/cancel`, { reason }); toast('排期已取消并留痕', 'warn'); U.reroute(); }
      catch (e) { toast(e.reason, 'error'); }
    };
  }

  function statusPill(i) {
    const map = { scheduled: 'amber', inspecting: 'amber', done: 'green', abnormal: 'red', cancelled: 'gray' };
    return `<span class="dot ${map[i.status] || 'gray'}">${esc(i.status_label)}</span>
      ${i.locked ? '<span class="dot gray">已锁定</span>' : ''}`;
  }

  function openFinish(id) {
    const m = modal({
      title: '登记查验结果',
      body: `
        <label>查验结果</label>
        <textarea id="f-result" rows="3" placeholder="如：开箱核对品名、数量、规格与申报一致，封识完好。"></textarea>
        <label style="margin-top:10px;display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="f-abn" style="width:auto"/> 查验异常（需退回重新审单）</label>`,
      footer: '<button class="btn" id="f-cancel">取消</button><button class="btn solid" id="f-ok">提交结果</button>',
    });
    m.mask.querySelector('#f-cancel').onclick = m.close;
    m.mask.querySelector('#f-ok').onclick = async () => {
      const result = m.mask.querySelector('#f-result').value.trim() || '查验无误';
      const abnormal = m.mask.querySelector('#f-abn').checked;
      try {
        await Api.post(`/inspections/${id}/finish`, { result, abnormal });
        m.close(); toast(abnormal ? '已登记查验异常，可在报关单转重审' : '查验完成，可放行', 'success'); U.reroute();
      } catch (e) { toast(e.reason, 'error'); }
    };
  }

  /* ================= 改派（预览 → 确认链式方案） ================= */

  async function openReassign(insp) {
    let yards = state.data ? [state.data.yard] : await Api.get('/yards');
    const yard = yards.find(y => y.id === insp.yard_id) || yards[0];
    if (!yard) { toast('该排期未关联场站，无法改派', 'error'); return; }
    const defStart = new Date(parseLocal(insp.scheduled_at).getTime() + 60 * 60000);

    const m = modal({
      title: `🔀 改派 · ${insp.decl_no}`, wide: true,
      body: `
        <div class="warn-box">从当前单解绑并重排，<b>同一场站后续单的计划查验时间将链式顺延</b>；
          已放行/已结关单不会移动；顺延只会向后，不存在环形依赖。</div>
        <div class="form-grid">
          <div><label>改派原因类型</label>
            <select id="r-type">
              <option value="bay_broken">🚛 车位故障（车故障）</option>
              <option value="inspector_leave">🙋 查验员请假</option>
              <option value="manual" selected>🛠️ 人工调整</option>
            </select></div>
          <div><label>新计划开始时间（当前单）</label>
            <input type="datetime-local" id="r-time" value="${toLocalInput(defStart)}" step="1800"/></div>
          <div><label>新车位（留空=自动选合规车位）</label>
            <select id="r-bay"><option value="">自动选择</option>
              ${yard.bays.map(b => `<option value="${b.id}" ${b.id === insp.bay_id ? 'selected' : ''}
                ${b.out_of_service ? 'disabled' : ''}>${esc(b.name)} ${b.out_of_service ? '（故障停用）' : ''}</option>`).join('')}
            </select></div>
          <div><label>新查验员（留空=自动选合规查验员）</label>
            <select id="r-insp"><option value="">自动选择</option>
              ${yard.inspectors.map(u => `<option value="${u.id}" ${u.id === insp.inspector_id ? 'selected' : ''}
                ${!u.available ? 'disabled' : ''}>${esc(u.display_name)} ${!u.available ? '（请假停用）' : ''}</option>`).join('')}
            </select></div>
          <div class="full"><label style="display:flex;align-items:center;gap:8px;font-weight:500;color:var(--gray-900)">
            <input type="checkbox" id="r-mark" style="width:auto"/> 同步登记资源不可用（故障车位停用 / 请假查验员停用）</label></div>
        </div>
        <div id="r-preview"></div>`,
      footer: `<button class="btn" id="r-cancel">取消</button>
               <button class="btn" id="r-prev">🔍 预览链式方案</button>
               <button class="btn solid" id="r-ok" disabled>确认改派并执行顺延</button>`,
    });

    let plan = null;
    function collect() {
      const type = m.mask.querySelector('#r-type').value;
      return {
        reason_type: type,
        new_scheduled_at: m.mask.querySelector('#r-time').value.replace('T', ' '),
        new_bay_id: Number(m.mask.querySelector('#r-bay').value) || null,
        new_inspector_id: Number(m.mask.querySelector('#r-insp').value) || null,
        duration_minutes: Math.max(30, Math.round((parseLocal(insp.scheduled_end) - parseLocal(insp.scheduled_at)) / 60000)),
        disable_bay_id: type === 'bay_broken' ? (insp.bay_id || null) : null,
        disable_inspector_id: type === 'inspector_leave' ? (insp.inspector_id || null) : null,
        mark_resource_unavailable: m.mask.querySelector('#r-mark').checked,
      };
    }

    m.mask.querySelector('#r-cancel').onclick = m.close;
    m.mask.querySelector('#r-prev').onclick = async () => {
      const c = collect();
      const box = m.mask.querySelector('#r-preview');
      box.innerHTML = '<div class="muted small">正在推演链式方案…</div>';
      try {
        const { new_scheduled_at, reason_type, new_bay_id, new_inspector_id, duration_minutes,
                disable_bay_id, disable_inspector_id } = c;
        const r = await Api.post(`/inspections/${insp.id}/reassign/preview`, {
          new_scheduled_at: new_scheduled_at.replace(' ', 'T'), reason_type,
          new_bay_id, new_inspector_id, duration_minutes, disable_bay_id, disable_inspector_id,
          reason: '预览',
        });
        plan = r.plan;
        box.innerHTML = renderPlan(plan);
        m.mask.querySelector('#r-ok').disabled = false;
      } catch (e) {
        plan = null;
        box.innerHTML = `<div class="danger-box">🚫 ${esc(e.reason)}</div>
          ${(e.data?.error?.details || []).map((c2, k) => `<div class="conf-item-card"><div class="ci-reason"><b>冲突${k + 1} ${esc(c2.title || '')}</b><br>${esc(c2.reason)}</div></div>`).join('')}`;
        m.mask.querySelector('#r-ok').disabled = true;
      }
    };

    m.mask.querySelector('#r-ok').onclick = async () => {
      if (!plan) return;
      const reason = await U.confirmNote({
        title: '确认执行链式改派', confirmText: '确认改派',
        message: `将解绑当前单并顺延 ${plan.moves.length - 1} 条后续单，阻塞 ${plan.blocked.length} 条。操作逐条留痕，确认？`,
        placeholder: '请填写改派原因（≥2字），如：重机位液压平台故障，预计停用一天',
      });
      if (!reason) return;
      if (reason.length < 5) { toast('改派原因不少于 5 个字（需留痕审计）', 'warn'); return; }
      const c = collect();
      try {
        const r = await Api.post(`/inspections/${insp.id}/reassign/confirm`, {
          ...c, new_scheduled_at: c.new_scheduled_at.replace(' ', 'T'), reason,
        });
        m.close();
        modal({
          title: '✅ 链式改派完成', wide: true,
          body: `<div class="warn-box">${esc(r.hint)}</div>${renderPlan(r.plan)}`,
          footer: '<button class="btn solid" id="ok">查看最新甘特</button>',
        }).mask.querySelector('#ok').onclick = () => U.reroute();
      } catch (e) {
        toast(e.reason, 'error');
      }
    };
  }

  function renderPlan(plan) {
    const moves = plan.moves.map((mv, i) => `
      <tr>
        <td>${mv.is_anchor ? '<span class="dot red">当前单</span>' : `<span class="muted">${i + 1}</span>`}</td>
        <td class="mono">${esc(mv.decl_no)}</td>
        <td>${esc(mv.cargo_name || '')}</td>
        <td>${esc((mv.from_scheduled_at || '').replace('T', ' ').slice(5))} <span class="muted">→</span> <b>${esc(mv.to_scheduled_at.replace('T', ' ').slice(5))}</b></td>
        <td>${esc(mv.from_bay_name || '—')} <span class="muted">→</span> ${esc(mv.to_bay_name || '—')}</td>
        <td>${esc(mv.from_inspector_name || '—')} <span class="muted">→</span> ${esc(mv.to_inspector_name || '—')}</td>
      </tr>`).join('');
    return `
      <div class="plan-summary">
        <span class="dot green">顺延/调整 ${plan.moves.length} 单</span>
        <span class="dot ${plan.blocked.length ? 'red' : 'gray'}">阻塞需人工 ${plan.blocked.length}</span>
        <span class="dot gray">终态未动 ${plan.skipped_terminal.length}</span>
      </div>
      <div class="table-wrap"><table class="data plan-table">
        <thead><tr><th></th><th>报关单</th><th>货物</th><th>计划时间</th><th>车位</th><th>查验员</th></tr></thead>
        <tbody>${moves}</tbody>
      </table></div>
      ${plan.blocked.length ? `<h4 class="log-title">⛔ 无法自动顺延（需人工处理）</h4>
        ${plan.blocked.map(b => `<div class="danger-box"><b class="mono">${esc(b.decl_no)}</b>（${esc(b.cargo_name || '')}）：${esc(b.reason)}</div>`).join('')}` : ''}
      ${plan.skipped_terminal.length ? `<h4 class="log-title">🔒 已放行/已结关单（未移动）</h4>
        <div>${plan.skipped_terminal.map(s => `<span class="pill rejected" style="margin:2px 6px 2px 0">${esc(s.decl_no)} ${esc(s.status_label)}</span>`).join('')}</div>` : ''}`;
  }

  /* ================= 安排查验（新建排期） ================= */

  window.BGTInspection = window.BGTInspection || {};
  window.BGTInspection.openSchedule = openSchedule;

  async function openSchedule(decl) {
    const yards = await Api.get('/yards');
    const m = modal({
      title: `📌 安排查验 · ${decl.decl_no}`, wide: true,
      body: `<div class="muted small">货物：${esc(decl.cargo_name)} · HS ${esc(decl.hs_code)} · 口岸 ${esc(decl.port)}</div>
        <div class="form-grid" style="margin-top:10px">
          <div><label>监管场站</label><select id="s-yard">${yards.map(y =>
            `<option value="${y.id}" ${y.port === decl.port ? 'selected' : ''}>${esc(y.name)}</option>`).join('')}</select></div>
          <div><label>时长（分钟）</label><input type="number" id="s-dur" value="120" step="30" min="30"/></div>
          <div><label>开始时间</label><input type="datetime-local" id="s-time" step="1800"/></div>
          <div><label>应查验日（及时率锚点）</label><input type="datetime-local" id="s-due" step="1800"/></div>
          <div><label>查验车位</label><select id="s-bay"></select></div>
          <div><label>查验员</label><select id="s-insp"></select></div>
        </div>
        <div style="margin:8px 0">
          <button class="btn sm" id="s-suggest">🎯 推荐最早空闲时段</button>
          <span id="s-certs" class="muted small" style="margin-left:8px"></span>
        </div>
        <div id="s-pre"></div>
        <div id="s-win"></div>`,
      footer: '<button class="btn" id="s-cancel">取消</button><button class="btn solid" id="s-ok">确认排期</button>',
    });
    let yardDetail = null, requiredLabels = [];
    async function refreshYard(preselect) {
      const yardId = Number(m.mask.querySelector('#s-yard').value);
      yardDetail = yards.find(y => y.id === yardId);
      const baySel = m.mask.querySelector('#s-bay');
      const inspSel = m.mask.querySelector('#s-insp');
      baySel.innerHTML = yardDetail.bays.map(b =>
        `<option value="${b.id}" ${b.out_of_service ? 'disabled' : ''}>${esc(b.name)} [${b.cert_tags.map(certLabel).join('/')}]${b.out_of_service ? '（故障）' : ''}</option>`).join('');
      inspSel.innerHTML = yardDetail.inspectors.map(u =>
        `<option value="${u.id}" ${!u.available ? 'disabled' : ''}>${esc(u.display_name)} [${u.certs.map(certLabel).join('/')}]${!u.available ? '（请假）' : ''}</option>`).join('');
      if (preselect) {
        baySel.value = String(preselect.bay_id || baySel.value);
        inspSel.value = String(preselect.inspector_id || inspSel.value);
      }
      try {
        const sug = await Api.get(`/scheduling/suggest?yard_id=${yardId}&declaration_id=${decl.id}`);
        requiredLabels = sug.required_cert_labels || [];
        m.mask.querySelector('#s-certs').textContent = '该单需要资质：' + requiredLabels.join('、');
      } catch { /* ignore */ }
    }
    function defaultTime() {
      const d = new Date(Date.now() + 3600 * 1000);
      d.setMinutes(d.getMinutes() < 30 ? 30 : 0, 0, 0);
      if (d.getMinutes() === 0 && new Date().getMinutes() >= 30) d.setHours(d.getHours() + 1);
      return d;
    }
    const t0 = defaultTime();
    m.mask.querySelector('#s-time').value = toLocalInput(t0);
    m.mask.querySelector('#s-due').value = toLocalInput(t0);
    await refreshYard();
    m.mask.querySelector('#s-yard').onchange = () => refreshYard();

    function bodyJson(extra = {}) {
      const t = m.mask.querySelector('#s-time').value;
      const dur = Number(m.mask.querySelector('#s-dur').value || 120);
      const end = new Date(t); end.setMinutes(end.getMinutes() + dur);
      return {
        declaration_id: decl.id, yard_id: Number(m.mask.querySelector('#s-yard').value),
        bay_id: Number(m.mask.querySelector('#s-bay').value),
        inspector_id: Number(m.mask.querySelector('#s-insp').value),
        scheduled_at: t, duration_minutes: dur,
        due_at: m.mask.querySelector('#s-due').value || t,
        ...extra,
      };
    }
    async function preflight() {
      try {
        const r = await Api.post('/inspections/conflicts', bodyJson());
        const box = m.mask.querySelector('#s-pre');
        const win = m.mask.querySelector('#s-win');
        win.innerHTML = '';
        if (r.hard_conflicts.length) {
          box.innerHTML = `<div class="danger-box">存在 ${r.hard_conflicts.length} 条硬冲突，当前不能排期：</div>` +
            r.hard_conflicts.map((c, k) => `<div class="conf-item-card"><div class="ci-head"><span class="ci-badge">冲突${k + 1}</span><b>${esc(c.title)}</b></div><div class="ci-reason">${esc(c.reason)}</div>
              ${c.related ? `<div class="ci-related" data-decl="${c.related.declaration_id}">🔗 ${esc(c.related.decl_no)} <span class="go">查看 ▸</span></div>` : ''}</div>`).join('');
          box.querySelectorAll('[data-decl]').forEach(x => x.onclick = () => { m.close(); location.hash = `#/declarations/${x.dataset.decl}`; });
          return false;
        }
        box.innerHTML = '<div class="ok-box">✅ 车位与查验员在该时段均空闲、资质匹配，可以排期。</div>';
        if (r.window_warnings.length) {
          win.innerHTML = `<div class="warn-box">⚠ ${r.window_warnings.map(w => esc(w.title)).join('；')}：需勾选确认并填写原因。</div>
            <label style="display:flex;gap:8px;align-items:center;font-weight:500;color:var(--gray-900)"><input type="checkbox" id="s-win-ok" style="width:auto"/> 我已确认超窗口排期</label>
            <textarea id="s-win-reason" rows="2" placeholder="超窗口原因（≥5字），将留痕"></textarea>`;
        }
        return true;
      } catch (e) {
        m.mask.querySelector('#s-pre').innerHTML = `<div class="danger-box">${esc(e.reason)}</div>`;
        return false;
      }
    }
    ['s-time', 's-bay', 's-insp', 's-dur'].forEach(id => {
      const node = m.mask.querySelector('#' + id);
      node.addEventListener('change', preflight);
    });
    m.mask.querySelector('#s-suggest').onclick = async () => {
      const yardId = Number(m.mask.querySelector('#s-yard').value);
      try {
        const r = await Api.get(`/scheduling/suggest?yard_id=${yardId}&declaration_id=${decl.id}&duration_minutes=${m.mask.querySelector('#s-dur').value}`);
        if (!r.slots.length) { toast('近期窗口无空闲组合，请改选场站或扩大时间', 'warn'); return; }
        const s0 = r.slots[0];
        m.mask.querySelector('#s-time').value = s0.scheduled_at.replace(' ', 'T').slice(0, 16);
        m.mask.querySelector('#s-bay').value = s0.bay_id;
        m.mask.querySelector('#s-insp').value = s0.inspector_id;
        toast(`已填入推荐时段：${s0.bay_name} / ${s0.inspector_name}`, 'success');
        preflight();
      } catch (e) { toast(e.reason, 'error'); }
    };
    m.mask.querySelector('#s-cancel').onclick = m.close;
    m.mask.querySelector('#s-ok').onclick = async () => {
      const ok = await preflight();
      if (!ok) return;
      const payload = bodyJson();
      const winChk = m.mask.querySelector('#s-win-ok');
      if (winChk) {
        const reason = (m.mask.querySelector('#s-win-reason').value || '').trim();
        if (!winChk.checked || reason.length < 5) { toast('超窗口排期需勾选并填写≥5字原因', 'warn'); return; }
        payload.window_confirmed = true; payload.window_reason = reason;
      }
      try {
        const r = await Api.post('/inspections', payload);
        m.close(); toast('查验排期已创建', 'success');
        location.hash = `#/inspections?yard=${r.inspection.yard_id}&inspection=${r.inspection.id}`;
      } catch (e) {
        if (e.code === 'schedule_conflict') {
          m.mask.querySelector('#s-pre').innerHTML =
            `<div class="danger-box">${esc(e.reason)}</div>` +
            (e.data?.error?.details || []).map((c, k) => `<div class="conf-item-card"><div class="ci-reason"><b>冲突${k + 1} ${esc(c.title || '')}</b><br>${esc(c.reason)}</div></div>`).join('');
        } else toast(e.reason, 'error');
      }
    };
    setTimeout(preflight, 100);
  }

  /* ================= 逻辑审核 ================= */

  window.BGTInspection.openReview = async function (decl) {
    const m = modal({
      title: `🧑‍⚖️ 逻辑审核 · ${decl.decl_no}`,
      body: `
        <div class="form-grid">
          <div class="full"><label>审核结论</label>
            <div class="review-opts">
              <label class="ro"><input type="radio" name="rv" value="pass_inspect" checked/> <b>通过 · 布控查验</b><span class="muted small">单证/逻辑均通过，命中布控，转安排查验</span></label>
              <label class="ro"><input type="radio" name="rv" value="release"/> <b>审结放行</b><span class="muted small">无需查验，直接放行</span></label>
              <label class="ro"><input type="radio" name="rv" value="return"/> <b>退回补录</b><span class="muted small">审核发现问题，退回报关员修正</span></label>
            </div>
          </div>
          <div><label style="display:flex;gap:8px;align-items:center;font-weight:500;color:var(--gray-900)"><input type="checkbox" id="rv-doc" checked style="width:auto"/> 单证一致性审核通过</label></div>
          <div><label style="display:flex;gap:8px;align-items:center;font-weight:500;color:var(--gray-900)"><input type="checkbox" id="rv-logic" checked style="width:auto"/> 归类/价格逻辑审核通过</label></div>
          <div class="full"><label>命中风控点（逗号分隔，可留空）</label><input id="rv-tags" placeholder="如：价格核查,原产地证待补"/></div>
          <div class="full"><label>审核意见</label><textarea id="rv-opinion" rows="3" placeholder="单证一致、归类逻辑无误，命中布控，转查验。"></textarea></div>
        </div>`,
      footer: '<button class="btn" id="rv-cancel">取消</button><button class="btn solid" id="rv-ok">提交审核结论</button>',
    });
    m.mask.querySelector('#rv-cancel').onclick = m.close;
    m.mask.querySelector('#rv-ok').onclick = async () => {
      const result = m.mask.querySelector('input[name=rv]:checked').value;
      const payload = {
        declaration_id: decl.id, result,
        document_ok: m.mask.querySelector('#rv-doc').checked,
        logic_ok: m.mask.querySelector('#rv-logic').checked,
        risk_tags: m.mask.querySelector('#rv-tags').value.trim(),
        opinion: m.mask.querySelector('#rv-opinion').value.trim(),
      };
      try {
        const r = await Api.post('/review-decisions', payload);
        m.close();
        toast(r.hint || ({ release: '已审结放行', return: '已退回补录' }[result] || '审核结论已提交'), 'success');
        U.reroute();
      } catch (e) { toast(e.reason, 'error'); }
    };
  };

  /* ================= 详情页查验卡（三种空态分开） ================= */

  window.BGTInspection.mountDetailCard = async function (view, data, opts) {
    const slot = view.querySelector('#inspection-slot');
    if (!slot) return;
    const d = data.declaration;
    const role = Api.user.role;
    const canManage = ['customs', 'supervisor'].includes(role);
    slot.innerHTML = '<div class="muted small" style="padding:6px 0">查验排期加载中…</div>';

    let rows;
    try {
      rows = await Api.get(`/inspections?declaration_id=${d.id}`);
    } catch (e) {
      // 空态 1：加载失败 —— 明确错误码/原因/重试，绝不伪装成「暂无数据」
      slot.innerHTML = '';
      slot.appendChild(el(`<div class="mini-error">
        <div class="me-head">⚠ 查验排期加载失败</div>
        <div class="me-reason"><b>错误码：</b>${esc(e.code || 'error')}<br/><b>原因：</b>${esc(e.reason || '网络异常')}</div>
        <button class="btn sm" id="me-retry">重试加载</button></div>`));
      slot.querySelector('#me-retry').onclick = () => window.BGTInspection.mountDetailCard(view, data, opts);
      return;
    }

    const active = rows.filter(r => ['scheduled', 'inspecting'].includes(r.status));
    const cancelled = rows.filter(r => r.status === 'cancelled');

    // 空态 3：有排期但全部取消
    if (rows.length && !active.length && cancelled.length === rows.length) {
      slot.innerHTML = `
        <div class="schedule-all-cancel">
          <div class="sac-title">🚫 该单 ${rows.length} 次查验排期均已取消，当前无有效排期</div>
          <div class="muted small">已取消排期不占用车位与查验员。取消记录：</div>
          <div class="sac-list">${rows.map(r => `<div>· ${fmtDate(r.scheduled_at)} ${esc(r.yard_name || '')} ${esc(r.bay || '')}
            <span class="muted">（状态：${esc(r.status_label)}）</span></div>`).join('')}</div>
          ${canManage ? `<button class="btn gold sm" id="sac-resched">＋ 重新安排查验</button>`
            : '<div class="muted small">请联系海关审单员重新安排查验。</div>'}
        </div>`;
      const b = slot.querySelector('#sac-resched');
      if (b) b.onclick = () => openSchedule(d);
      return;
    }

    // 空态 2：该单从未有过排期（200 空列表）
    if (!rows.length) {
      if (d.status === 'reviewing') {
        const hasPass = (data.review_decisions || []).some(x => x.result === 'pass_inspect');
        slot.innerHTML = `
          <div class="schedule-empty">
            <div class="se-title">🧭 该单暂无查验排期</div>
            <div class="muted small">${hasPass
              ? '逻辑审核已通过布控，尚未在监管场站排期。'
              : '需海关审单员先完成「逻辑审核通过（布控查验）」，再安排查验。'}</div>
            <div style="display:flex;gap:8px;margin-top:8px">
              ${canManage && !hasPass ? '<button class="btn solid sm" id="se-review">🧑‍⚖️ 逻辑审核</button>' : ''}
              ${canManage && hasPass ? '<button class="btn gold sm" id="se-sched">📌 安排查验</button>' : ''}
            </div>
          </div>`;
        const rb = slot.querySelector('#se-review'); if (rb) rb.onclick = () => window.BGTInspection.openReview(d);
        const sb = slot.querySelector('#se-sched'); if (sb) sb.onclick = () => openSchedule(d);
      } else if (d.status === 'inspecting' && canManage) {
        slot.innerHTML = `<div class="schedule-empty">
          <div class="se-title">🧭 该单暂无查验排期</div>
          <button class="btn gold sm" id="se-sched2">📌 补排查验计划</button></div>`;
        slot.querySelector('#se-sched2').onclick = () => openSchedule(d);
      } else {
        slot.innerHTML = `<div class="schedule-empty"><div class="se-title">🧭 该单暂无查验排期</div>
          <div class="muted small">${esc(d.status_label)}环节无查验任务记录。</div></div>`;
      }
      return;
    }

    // 正常：排期卡列表
    slot.innerHTML = `<div class="insp-cards">
      ${rows.map(r => `
        <div class="insp-mini ${r.conflicts && r.conflicts.length ? 'has-conflict' : ''}" data-iid="${r.id}">
          <div class="im-top">
            ${statusPill(r)}
            ${r.conflicts && r.conflicts.length ? `<span class="dot red">${r.conflicts.length} 条冲突</span>` : ''}
            ${r.locked ? '<span class="dot gray">锁定</span>' : ''}
            <span class="muted small" style="margin-left:auto">${esc(r.yard_name || '')}</span>
          </div>
          <div class="im-main">📅 ${fmtDate(r.scheduled_at)} – ${esc((r.scheduled_end || '').slice(11))}
            ｜🅿️ ${esc(r.bay || '—')}｜🙋 ${esc(r.inspector_name || '未指派')}</div>
          <div class="muted small">应查验日 ${fmtDate(r.due_at)} · 资质 ${(r.required_certs || []).map(certLabel).join('/')}
            ${r.result_note ? '· ' + esc(r.result_note) : ''}</div>
        </div>`).join('')}
    </div>
    ${canManage && d.status === 'reviewing' ? '<button class="btn solid sm" id="dc-review" style="margin-top:8px">🧑‍⚖️ 逻辑审核</button>' : ''}
    ${canManage && d.status === 'inspecting' ? '<button class="btn gold sm" id="dc-sched" style="margin-top:8px">＋ 追加排期</button>' : ''}`;
    slot.querySelectorAll('.insp-mini').forEach(c => c.onclick = () => openInspection(Number(c.dataset.iid)));
    const rv = slot.querySelector('#dc-review'); if (rv) rv.onclick = () => window.BGTInspection.openReview(d);
    const sc = slot.querySelector('#dc-sched'); if (sc) sc.onclick = () => openSchedule(d);
  };

  /* ================= 及时率双口径 ================= */

  async function openTimeliness() {
    let month = '';
    const m = modal({
      title: '📊 查验及时率（双口径，请勿混用）', wide: true,
      body: '<div class="muted">加载中…</div>',
      footer: '<button class="btn solid" id="tm-close">关闭</button>',
    });
    m.mask.querySelector('#tm-close').onclick = m.close;
    async function load() {
      const r = await Api.get('/metrics/timeliness' + (month ? '?month=' + month : ''));
      const t = r.calibers.time, v = r.calibers.volume;
      const card = (c, primary) => `
        <div class="caliber ${primary ? 'primary' : ''}" data-caliber="${c.key}">
          <div class="cb-name">${esc(c.name)}</div>
          <div class="cb-rate">${c.rate_percent == null ? '—' : c.rate_percent + '%'}</div>
          <div class="cb-frac">${c.numerator} / ${c.denominator}</div>
          <div class="cb-formula">${esc(c.formula)}</div>
        </div>`;
      const maxRate = Math.max(10, ...r.months.map(x => x.time_rate || 0).concat(r.months.map(x => x.volume_rate || 0)));
      m.mask.querySelector('.modal-body').innerHTML = `
        <div class="warn-box">📌 ${esc(r.notice)}</div>
        <div class="tm-toolbar">
          <label>统计月份</label>
          <input type="month" id="tm-month" value="${r.month}"/>
        </div>
        <div class="calibers">${card(t, true)}${card(v, false)}</div>
        <div class="muted small">本月改派/链式顺延留痕 <b>${r.calibers.reassign_logs}</b> 条；
          频繁改派月份：实际查验日后推 → 时效口径走低；改派后仍完成 → 单量口径可能保持高位，两口径方向相反属正常。</div>
        <h4 class="log-title">近 6 个月两口径走势</h4>
        <div class="trend">
          ${r.months.map(x => `
            <div class="tr-month">
              <div class="tr-label">${x.month}${x.reassign_logs ? `<span class="tr-reassign" title="改派留痕条数">🔀${x.reassign_logs}</span>` : ''}</div>
              <div class="tr-bars">
                <div class="tr-bar time" style="height:${(x.time_rate || 0) / maxRate * 90}px" title="时效 ${x.time_rate ?? '—'}%"></div>
                <div class="tr-bar volume" style="height:${(x.volume_rate || 0) / maxRate * 90}px" title="单量 ${x.volume_rate ?? '—'}%"></div>
              </div>
              <div class="tr-vals"><span class="t">${x.time_rate == null ? '—' : x.time_rate + '%'}</span><span class="v">${x.volume_rate == null ? '—' : x.volume_rate + '%'}</span></div>
            </div>`).join('')}
        </div>
        <div class="trend-legend"><span><i class="lg time"></i>时效口径</span><span><i class="lg volume"></i>单量口径</span></div>`;
      m.mask.querySelector('#tm-month').onchange = e => { month = e.target.value; load().catch(err => toast(err.reason, 'error')); };
    }
    try { await load(); } catch (e) { m.close(); toast(e.reason, 'error'); }
  }

  /* ================= 通用错误态 ================= */

  function errorState(e, title, onRetry) {
    const reason = e && e.reason ? e.reason : '服务暂时不可用，请稍后重试。';
    const code = e && e.code ? e.code : 'network_error';
    const box = el(`
      <div style="padding:24px;max-width:720px;margin:0 auto">
        <div class="error-state">
          <div class="ico">🔌</div>
          <h3>${esc(title)}</h3>
          <div class="reason"><b>错误码：</b>${esc(code)}<br/><b>原因：</b>${esc(reason)}</div>
          <button class="btn solid" id="retry">重新加载</button>
        </div>
      </div>`);
    box.querySelector('#retry').onclick = onRetry;
    return box;
  }
})();
