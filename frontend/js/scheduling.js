/* 查验排期模块：周/日甘特、拖拽落位、逐条冲突、改派链式重排、三种空态、双口径及时率。 */
(function () {
  'use strict';

  let ctx = null;
  const { Api } = window;

  // 甘特时间轴：每天展示 07:00–20:00（营业窗口均落在其内，窗口外用深色底标出）
  const AXIS_START = 7, AXIS_END = 20, AXIS_HOURS = AXIS_END - AXIS_START;
  const RES_NAME_W = 176;

  // 模块状态
  const state = {
    resources: null,
    yardId: null,
    mode: 'week',          // week / day
    anchor: startOfWeek(new Date()),
    gantt: null,           // 当前日历数据
    loading: false,
    loadError: null,
    month: monthStr(new Date()),
  };

  /* ---------------- 小工具 ---------------- */

  function startOfWeek(d) {
    const x = new Date(d);
    const wd = (x.getDay() + 6) % 7; // 周一为 0
    x.setDate(x.getDate() - wd);
    x.setHours(0, 0, 0, 0);
    return x;
  }
  function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
  function ymd(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function monthStr(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
  function parseDt(s) { return s ? new Date(s) : null; }
  function hm(d) { return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; }
  function rangeOf() {
    const days = state.mode === 'day' ? 1 : 7;
    const start = state.mode === 'day' ? new Date(state.anchor) : startOfWeek(state.anchor);
    start.setHours(0, 0, 0, 0);
    const end = addDays(start, days);
    return { start, end, days };
  }
  function roundHalf(d) {
    const m = d.getMinutes() < 30 ? 0 : 30;
    d.setMinutes(m, 0, 0);
    return d;
  }
  function canWrite() {
    return ['customs', 'supervisor'].includes(Api.user.role);
  }

  /* ---------------- 注册 ---------------- */

  window.SchedulingModule = {
    register(appCtx) {
      ctx = appCtx;
      ctx.route('/scheduling', () => page(ctx.shell('/scheduling')));
    },
    bindDetail,
    openScheduleModal,
  };

  /* ================= 主页面 ================= */

  async function page(view) {
    view.innerHTML = ctx.loading();
    try {
      if (!state.resources) state.resources = await Api.get('/scheduling/resources');
      if (!state.yardId) state.yardId = state.resources.yards[0]?.id;
      await loadGantt();
      render(view);
    } catch (e) {
      renderLoadError(view, e);
    }
  }

  async function loadGantt() {
    const { start, end } = rangeOf();
    state.loadError = null;
    state.gantt = await Api.get(
      `/scheduling/calendar?yard_id=${state.yardId}&start=${ymd(start)}T00:00&end=${ymd(end)}T00:00`);
  }

  // —— 空态/错误态之一：加载失败 ——（与「无排期」「全取消」严格分开，不统一写「暂无数据」）
  function renderLoadError(view, e) {
    const offline = e.code === 'network_offline';
    view.innerHTML = '';
    const box = ctx.el(`
      <div class="state-block error">
        <div class="ico">${offline ? '📵' : '⚠️'}</div>
        <h3>${offline ? '口岸离线，查验排期加载失败' : '查验排期加载失败'}</h3>
        <div class="state-desc">
          ${offline
            ? '当前网络不可达，无法获取监管场站的实时排期。为避免用旧排期误导现场作业，本页不展示缓存冒充数据；联网后点击重试。'
            : `服务暂不可用（错误码：${e.code || 'unknown'}）。${e.reason || '请稍后重试。'}`}
        </div>
        <button class="btn solid" id="retry">🔄 重新加载</button>
      </div>`);
    box.querySelector('#retry').onclick = () => page(view);
    view.appendChild(box);
  }

  function yard() { return state.resources.yards.find(y => y.id === state.yardId); }

  function render(view) {
    const y = yard();
    const { start, days } = rangeOf();
    const g = state.gantt;

    view.innerHTML = `
      <div class="page-head">
        <div><h2>🗓️ 查验排期</h2>
          <div class="desc">监管场站车位 / 查验员有限资源甘特 · 拖拽落位 · 冲突逐条标红 · 改派链式顺延
            <span class="muted small">（营业窗口 ${fmtWindow(y)}）</span></div>
        </div>
      </div>

      <div class="sched-toolbar">
        <div class="fg" style="display:flex;flex-direction:column">
          <label>监管场站</label>
          <select id="sc-yard">${state.resources.yards.map(yy =>
            `<option value="${yy.id}" ${yy.id === state.yardId ? 'selected' : ''}>${yy.name}（${yy.port}）</option>`).join('')}</select>
        </div>
        <div class="seg" id="sc-mode">
          <button data-mode="day" class="${state.mode === 'day' ? 'on' : ''}">日视图</button>
          <button data-mode="week" class="${state.mode === 'week' ? 'on' : ''}">周视图</button>
        </div>
        <div class="seg" id="sc-pager">
          <button id="sc-prev">‹ 前${state.mode === 'day' ? '一天' : '一周'}</button>
          <button id="sc-today">今天</button>
          <button id="sc-next">后${state.mode === 'day' ? '一天' : '一周'} ›</button>
        </div>
        <div class="muted small" id="sc-range" style="font-weight:600"></div>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" id="sc-changes">📜 排期留痕</button>
          <button class="btn solid" id="sc-refresh">🔄 刷新</button>
        </div>
      </div>

      <div id="sc-gantt-slot"></div>

      <div class="card" style="margin-top:16px">
        <h3>📈 查验及时率 <span class="cal-toggle" id="sc-cal-note" style="margin-left:auto">口径说明 ▾</span></h3>
        <div id="sc-timeliness"></div>
      </div>`;

    const rangeText = state.mode === 'day'
      ? ymd(start)
      : `${ymd(start)} ~ ${ymd(addDays(start, 6))}`;
    view.querySelector('#sc-range').textContent = rangeText;

    view.querySelector('#sc-yard').onchange = e => {
      state.yardId = Number(e.target.value); refresh(view);
    };
    view.querySelector('#sc-mode').querySelectorAll('button').forEach(b => b.onclick = () => {
      state.mode = b.dataset.mode; render(view);
    });
    view.querySelector('#sc-prev').onclick = () => { state.anchor = addDays(state.anchor, state.mode === 'day' ? -1 : -7); refresh(view); };
    view.querySelector('#sc-next').onclick = () => { state.anchor = addDays(state.anchor, state.mode === 'day' ? 1 : 7); refresh(view); };
    view.querySelector('#sc-today').onclick = () => { state.anchor = new Date(); refresh(view); };
    view.querySelector('#sc-refresh').onclick = () => refresh(view);
    view.querySelector('#sc-changes').onclick = () => openChanges(y);
    view.querySelector('#sc-cal-note').onclick = () => {
      const d = view.querySelector('#sc-cal-detail');
      if (d) d.classList.toggle('open');
    };

    renderGantt(view.querySelector('#sc-gantt-slot'), y, g, days, start, view);
    renderTimeliness(view.querySelector('#sc-timeliness'), view);
  }

  async function refresh(view) {
    view.querySelector('#sc-gantt-slot').innerHTML = ctx.loading();
    try { await loadGantt(); render(view); }
    catch (e) { renderLoadError(view, e); }
  }

  function fmtWindow(y) {
    const h = v => `${Math.floor(v)}:${String(Math.round((v % 1) * 60)).padStart(2, '0')}`;
    return `${h(y.open_hour)}–${h(y.close_hour)} / ${y.work_weekend ? '全周作业' : '工作日'}`;
  }

  /* ================= 甘特渲染 ================= */

  function renderGantt(slot, y, g, days, rangeStart, view) {
    const list = g.inspections || [];
    // —— 空态之二：该场站该时段排期全部取消（有取消记录，无一条有效）——
    if (g.all_cancelled) {
      slot.innerHTML = '';
      slot.appendChild(ctx.el(`
        <div class="state-block cancelled">
          <div class="ico">🚫</div>
          <h3>该场站本时段查验排期已全部取消</h3>
          <div class="state-desc">共有 ${g.cancelled_count} 条排期被取消，当前没有任何待执行查验任务。
            取消可能由台位故障、资质不匹配或企业申请引起。下列为取消记录与逐条原因：</div>
          <div class="cancel-list">
            ${(g.cancelled || []).map(c => `
              <div class="cl-item">
                <b class="mono">${ctx.esc(c.decl_no)}</b> · ${ctx.esc(c.cargo_name)}
                · 原计划 ${ctx.fmtDate(c.scheduled_at)} · ${ctx.esc(c.bay_code)}
                <div style="color:var(--amber-700,var(--amber-600));margin-top:3px">取消原因：${ctx.esc(c.cancel_reason || '未填写')}</div>
              </div>`).join('')}
          </div>
          <button class="btn" id="sc-back">换个时间范围</button>
        </div>`));
      slot.querySelector('#sc-back').onclick = () => { state.anchor = addDays(state.anchor, state.mode === 'day' ? -7 : -7); refresh(view); };
      return;
    }
    // —— 空态之三：该场站该时段确实没有任何排期 ——
    if (!list.length) {
      slot.innerHTML = '';
      slot.appendChild(ctx.el(`
        <div class="state-block">
          <div class="ico">🗓️</div>
          <h3>该场站当前时段暂无查验排期</h3>
          <div class="state-desc">${y.name} 在所选${state.mode === 'day' ? '一天' : '一周'}内既无待执行任务，也无取消记录。
            海关审单员逻辑审核通过并布控后，可从报关单详情「安排查验」生成排期。</div>
          <button class="btn solid" id="sc-jump" ${canWrite() ? '' : 'disabled'}>去报关单安排查验</button>
        </div>`));
      slot.querySelector('#sc-jump').onclick = () => { location.hash = '#/declarations?status=reviewing'; };
      return;
    }

    const dayCols = g.days || [];
    // 表头
    const headDays = dayCols.map((d, i) => {
      const dt = addDays(rangeStart, i);
      const wk = ['一', '二', '三', '四', '五', '六', '日'][dt.getDay() === 0 ? 6 : dt.getDay() - 1];
      const isToday = ymd(dt) === ymd(new Date());
      return `<div class="gh-day ${!d.working ? 'we' : ''}">${d.date.slice(5)} 周${wk} ${isToday ? '· 今天' : ''}</div>`;
    }).join('');
    // 小时刻度：列坐标按全天 0–24h，与色块/落位换算一致
    const headHours = dayCols.map(() => `
      <div class="gh-hours">${[7, 9, 12, 15, 18, 20].map(h =>
        `<span style="left:${(h / 24 * 100).toFixed(2)}%">${h}</span>`).join('')}</div>`).join('');

    // 资源行：车位组 + 查验员组（查验员按场站所属口岸过滤，已在本场站有任务的也保留）
    const bays = y.bays || [];
    const usedInspIds = new Set(list.map(i2 => i2.inspector_id).filter(Boolean));
    const inspectors = (state.resources.inspectors || [])
      .filter(u => (u.port && u.port === y.port) || usedInspIds.has(u.id));
    const rowsHtml = [];
    rowsHtml.push(groupRow('查验车位', bays.length));
    bays.forEach(b => rowsHtml.push(resourceRow('bay', b, list, days, rangeStart, y)));
    rowsHtml.push(groupRow('查验员', inspectors.length));
    inspectors.forEach(u => rowsHtml.push(resourceRow('inspector', u, list, days, rangeStart, y)));

    slot.innerHTML = `
      <div class="card gantt-card">
        <div class="gantt-scroll">
          <div class="gantt">
            <div class="gantt-head" style="grid-template-columns:${RES_NAME_W}px repeat(${days},1fr)">
              <div class="gh-corner">资源 / 时间</div>
              ${headDays}
              ${headHours}
            </div>
            <div class="gantt-body" id="g-body">${rowsHtml.join('')}</div>
          </div>
        </div>
        <div class="small muted" style="padding:8px 14px;border-top:1px solid var(--gray-200);background:var(--gray-50)">
          🖱️ 拖动色块改期：拖到<b>车位行</b>换时间/车位，拖到<b>查验员行</b>换时间/查验员；落到营业窗口外需二次确认填原因。
          红框脉冲＝当前存在冲突（车位双占 / 查验员双占 / 故障 / 请假 / 资质不符），点色块看逐条原因。🔒＝已放行/结关锁定不可动。
        </div>
      </div>`;

    bindDnD(slot, y, view);
    slot.querySelectorAll('[data-insp]').forEach(el0 => {
      el0.onclick = e => {
        if (el0.dataset.dragged === '1') { el0.dataset.dragged = ''; return; }
        openInspection(Number(el0.dataset.insp), y, view);
      };
    });
    slot.querySelectorAll('[data-toggle-bay]').forEach(b => b.onclick = e => {
      e.stopPropagation(); toggleBay(Number(b.dataset.toggleBay), b.dataset.broken === '1', y, view);
    });
    slot.querySelectorAll('[data-toggle-insp]').forEach(b => b.onclick = e => {
      e.stopPropagation(); toggleInspector(Number(b.dataset.toggleInsp), b.dataset.leave === '1', y, view);
    });
  }

  function groupRow(title, n) {
    return `<div class="gantt-row" style="height:26px;background:var(--navy-800);cursor:default;grid-template-columns:${RES_NAME_W}px 1fr">
        <div class="g-resource" style="color:#fff;background:var(--navy-800);border-right:1px solid #2c4470">${title}（${n}）</div>
        <div class="g-track"></div></div>`;
  }

  function resourceRow(dim, r, list, days, rangeStart, y) {
    const isBay = dim === 'bay';
    const broken = isBay ? r.out_of_service : r.on_leave;
    const sub = isBay
      ? `${r.kind_label}${broken ? ' · ⚠️故障停用' : ''}`
      : `${qualBadges(r.qualifications)}${broken ? ' · ⚠️请假中' : ''}`;
    const toggle = canWrite()
      ? (isBay
          ? `<button class="btn sm" data-toggle-bay="${r.id}" data-broken="${broken ? 0 : 1}" style="padding:1px 6px">${broken ? '恢复' : '故障'}</button>`
          : `<button class="btn sm" data-toggle-insp="${r.id}" data-leave="${broken ? 0 : 1}" style="padding:1px 6px">${broken ? '销假' : '请假'}</button>`)
      : '';
    const nowLine = nowLineStyle(rangeStart, days);
    // 该资源上的任务（车位维度/人员维度各挂一份，双占一眼可见）
    const mine = list.filter(i2 => isBay ? i2.bay_id === r.id : i2.inspector_id === r.id);
    const blocks = mine.map(i2 => blockHtml(i2, days, rangeStart)).join('');
    const shading = dayShading(days, rangeStart, y);
    return `
      <div class="gantt-row" data-dim="${dim}" data-rid="${r.id}"
        style="grid-template-columns:${RES_NAME_W}px repeat(${days},1fr)">
        <div class="g-resource" style="${broken ? 'color:var(--red-600)' : ''}">
          <span style="min-width:0;overflow:hidden;text-overflow:ellipsis">${isBay ? '🅿️ ' : '🧑‍🔧 '}<b>${ctx.esc(isBay ? r.code : r.display_name)}</b>
            <span class="res-sub">${sub}</span></span>${toggle}
        </div>
        <div class="g-track">
          ${shading}${nowLine}${blocks}
        </div>
      </div>`;
  }

  function qualBadges(quals) {
    const labels = { chilled: '冷链', dg: '危化', large: '大型' };
    const qs = quals && quals.length ? quals : [];
    return `<span class="qual-badges">${qs.map(q => `<span class="qb ${q}">${labels[q] || q}</span>`).join('')
      || '<span class="qb none">普货</span>'}</span>`;
  }

  function dayShading(days, rangeStart, y) {
    let html = '';
    const pctTop = h => h / 24 * 100;  // 列坐标按全天 0–24h
    for (let i = 0; i < days; i++) {
      const d = addDays(rangeStart, i);
      const weekend = d.getDay() === 0 || d.getDay() === 6;
      const left = i / days * 100, w = 1 / days * 100;
      // 非作业日整日斜纹
      if (!y.work_weekend && weekend) {
        html += `<div style="position:absolute;top:0;bottom:0;left:${left}%;width:${w}%" class="g-col-we"></div>`;
        continue;
      }
      // 营业窗口外的早晚时段
      if (y.open_hour > 0)
        html += `<div style="position:absolute;top:0;height:${pctTop(y.open_hour)}%;left:${left}%;width:${w}%" class="g-col-off"></div>`;
      if (y.close_hour < 24)
        html += `<div style="position:absolute;top:${pctTop(y.close_hour)}%;bottom:0;left:${left}%;width:${w}%" class="g-col-off"></div>`;
    }
    // 天分隔线
    for (let i = 1; i < days; i++) {
      html += `<div style="position:absolute;top:0;bottom:0;left:${i / days * 100}%;width:1px;background:var(--gray-200)"></div>`;
    }
    return html;
  }

  function nowLineStyle(rangeStart, days) {
    const now = new Date();
    if (now < rangeStart || now > addDays(rangeStart, days)) return '';
    const totalMin = (now - rangeStart) / 60000;
    const left = totalMin / (days * 1440) * 100;
    return `<div class="g-nowline" style="left:${left}%" title="当前时刻"></div>`;
  }

  function blockHtml(i, days, rangeStart) {
    const s = parseDt(i.scheduled_at), e = parseDt(i.scheduled_end);
    const total = days * 1440 * 60000;
    const left = (s - rangeStart) / total * 100;
    const width = Math.max((e - s) / total * 100, 0.55);
    const conflict = i.has_conflict;
    const overdue = i.overdue;
    const locked = ['released', 'closed'].includes(i.decl_status);
    const cls = ['g-block', `st-${i.status}`, conflict ? 'conflict' : '', overdue ? 'overdue' : '', locked ? 'locked' : '']
      .filter(Boolean).join(' ');
    const draggable = canWrite() && !locked && ['pending', 'inspecting'].includes(i.status) ? 'draggable="true"' : '';
    return `<div class="${cls}" data-insp="${i.id}" ${draggable}
       style="left:${left}%;width:${width}%"
       title="${ctx.esc(i.decl_no)} ${ctx.esc(i.scheduled_at?.slice(11))} ${conflict ? '· 有冲突，点击查看' : ''}">
        <div class="gb-no">${ctx.esc(i.decl_no.slice(-5))} ${conflict ? '⚠️' : ''}</div>
        <div class="gb-meta">${ctx.esc(i.bay_code || '')} · ${ctx.esc((i.inspector_name || '').replace(/（.*?）/g, ''))}</div>
      </div>`;
  }

  /* ================= 拖拽落位 ================= */

  function bindDnD(slot, y, view) {
    let dragId = null, ghost = null;
    slot.querySelectorAll('.g-block[draggable="true"]').forEach(b => {
      b.addEventListener('dragstart', ev => {
        dragId = Number(b.dataset.insp);
        b.classList.add('g-dragging');
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', String(dragId));
      });
      b.addEventListener('dragend', () => {
        b.classList.remove('g-dragging');
        slot.querySelectorAll('.gantt-row.drop-hilite').forEach(r => r.classList.remove('drop-hilite'));
        if (ghost) { ghost.remove(); ghost = null; }
        setTimeout(() => { b.dataset.dragged = '1'; }, 0);
      });
    });
    slot.querySelectorAll('.gantt-row[data-dim]').forEach(row => {
      row.addEventListener('dragover', ev => {
        ev.preventDefault();
        row.classList.add('drop-hilite');
        const t = dropTime(ev, row, y);
        if (!ghost) ghost = ctx.el('<div class="g-dropghost"></div>');
        const track = row.querySelector('.g-track');
        if (!ghost.parentElement) track.appendChild(ghost);
        const { days } = rangeOf();
        const { start } = rangeOf();
        const total = days * 1440 * 60000;
        ghost.style.left = ((t - start) / total * 100) + '%';
        ghost.style.width = (120 / (days * 1440) * 100) + '%';
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop-hilite'));
      row.addEventListener('drop', async ev => {
        ev.preventDefault();
        row.classList.remove('drop-hilite');
        if (ghost) { ghost.remove(); ghost = null; }
        const id = Number(ev.dataTransfer.getData('text/plain') || dragId);
        const t = dropTime(ev, row, y);
        const dim = row.dataset.dim, rid = Number(row.dataset.rid);
        await doDrop(id, dim, rid, t, y, view);
      });
    });
  }

  function dropTime(ev, row, y) {
    const track = row.querySelector('.g-track');
    const rect = track.getBoundingClientRect();
    const { days, start } = rangeOf();
    const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
    const minutes = frac * days * 1440;
    const dayIdx = Math.min(days - 1, Math.floor(minutes / 1440));
    const inDay = minutes - dayIdx * 1440;  // 距当天 00:00 的分钟（列坐标按全天 0–24h）
    const d = addDays(start, dayIdx);
    d.setHours(0, 0, 0, 0);
    d.setMinutes(Math.round(inDay / 30) * 30);  // 30 分钟吸附
    return d;
  }

  async function doDrop(id, dim, rid, t, y, view) {
    const insp = state.gantt.inspections.find(x => x.id === id);
    if (!insp) return;
    const payload = {
      scheduled_at: toIsoMin(t),
      reason: '',
      confirm: false,
    };
    if (dim === 'bay') payload.bay_id = rid;
    else payload.inspector_id = rid;

    const submit = async (confirm, reason) => {
      payload.confirm = confirm;
      payload.reason = reason || '';
      return Api.post(`/scheduling/inspections/${id}/reschedule`, payload);
    };

    try {
      await submit(false);
      ctx.toast('改期成功', 'success');
      refresh(view);
    } catch (e) {
      if (e.code === 'need_confirm') {
        openDropConfirm(e.data.error.conflicts || [], t, async (reason) => {
          try { await submit(true, reason); ctx.toast('已按超窗口安排落位并留痕', 'success'); refresh(view); }
          catch (e2) { ctx.toast(e2.reason, 'error'); }
        });
      } else if (e.code === 'scheduling_conflict') {
        openConflictModal('改期被排期冲突拦下', e.data.error.conflicts || [], insp, y);
      } else {
        ctx.toast(e.reason, 'error');
      }
    }
  }

  function toIsoMin(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function openDropConfirm(conflicts, t, onOk) {
    const m = ctx.modal({
      title: '⚠️ 落点超出营业窗口 · 二次确认',
      body: `
        <div class="warn-box">拟落位时间 <b>${ctx.fmtDate(toIsoMin(t))}</b> 不在场站营业窗口内。
          非常规时段查验须值班负责人确认并填写原因，<b>本次确认将写入排期留痕</b>。</div>
        ${conflictListHtml(conflicts, true)}
        <label style="margin-top:10px">非常规安排原因（不少于 4 字，必填留痕）</label>
        <textarea id="dc-reason" rows="3" placeholder="如：冷链货物到港延误，企业申请夜间加班查验并已报备值班关员"></textarea>`,
      footer: '<button class="btn" id="dc-cancel">取消改期</button><button class="btn solid" id="dc-ok">确认超窗口落位</button>',
    });
    m.mask.querySelector('#dc-cancel').onclick = m.close;
    m.mask.querySelector('#dc-ok').onclick = () => {
      const reason = m.mask.querySelector('#dc-reason').value.trim();
      if (reason.length < 4) { ctx.toast('原因不少于 4 个字', 'warn'); return; }
      m.close(); onOk(reason);
    };
  }

  /* ================= 冲突清单（逐条标红、可展开看原因） ================= */

  function conflictListHtml(conflicts, openAll) {
    if (!conflicts || !conflicts.length) return '';
    return `<div class="conflict-list">${conflicts.map((c, i) => `
      <div class="conflict-item ${c.severity === 'warning' ? 'warning' : 'blocking'} ${openAll ? 'open' : ''}">
        <div class="ci-head" data-ci="${i}">
          <span>${c.severity === 'warning' ? '🟡' : '🔴'}</span>
          <span>${ctx.esc(c.title || c.code)}</span>
          <span class="ci-tag">${c.severity === 'warning' ? '需确认' : '阻断'}</span>
          <span class="ci-caret">▶</span>
        </div>
        <div class="ci-body">
          <div class="ci-reason">${ctx.esc(c.reason)}</div>
          ${c.conflict_with ? `<div class="ci-other">
            冲突对方：<b class="mono">${ctx.esc(c.conflict_with.decl_no)}</b> · ${ctx.esc(c.conflict_with.cargo_name || '')}
            · ${ctx.fmtDate(c.conflict_with.scheduled_at)}–${hm(parseDt(c.conflict_with.scheduled_end))}
            · 车位 ${ctx.esc(c.conflict_with.bay_code || '')} · 查验员 ${ctx.esc(c.conflict_with.inspector_name || '')}
            ${['released', 'closed'].includes(c.conflict_with.decl_status) ? ' · <b style="color:var(--red-600)">该单已锁定不可移动</b>' : ''}
          </div>` : ''}
          <div class="small muted" style="margin-top:4px">规则码：${ctx.esc(c.code)} · 类别：${ctx.esc(c.kind)}</div>
        </div>
      </div>`).join('')}</div>`;
  }

  function bindConflictToggle(root) {
    root.querySelectorAll('.ci-head').forEach(h => h.onclick = () => {
      h.parentElement.classList.toggle('open');
    });
  }

  function openConflictModal(title, conflicts, insp, y) {
    const m = ctx.modal({
      title: '🚫 ' + title,
      wide: true,
      body: `
        <div class="danger-box">该操作未执行、排期未变更。共检出 ${conflicts.length} 条冲突，
          <b>点开每一条可查看具体原因与冲突对方</b>，请据此改选车位 / 查验员 / 时间后重试。</div>
        ${conflictListHtml(conflicts)}
        <div class="small muted" style="margin-top:8px">提示：已放行/已结关单的查验时间锁定，
          不能移动它们来腾位；只能调整本单或走改派让后续单链式顺延。</div>`,
      footer: '<button class="btn solid" id="cm-ok">我知道了，去调整</button>',
    });
    bindConflictToggle(m.mask);
    m.mask.querySelector('#cm-ok').onclick = m.close;
  }

  /* ================= 色块详情 / 取消 / 改派入口 ================= */

  function openInspection(id, y, view) {
    const i = state.gantt.inspections.find(x => x.id === id);
    if (!i) return;
    const locked = ['released', 'closed'].includes(i.decl_status);
    const movable = canWrite() && !locked && ['pending', 'inspecting'].includes(i.status);
    const conflicts = i.current_conflicts || [];
    const m = ctx.modal({
      title: `查验任务 · ${i.decl_no}`,
      wide: true,
      body: `
        <div class="warn-box">
          <b>${ctx.esc(i.cargo_name)}</b>（HS ${ctx.esc(i.hs_code)}）<br/>
          计划：${ctx.fmtDate(i.scheduled_at)} – ${hm(parseDt(i.scheduled_end))}（${i.duration_minutes} 分钟）<br/>
          场站：${ctx.esc(i.yard_name)} · 车位 <b>${ctx.esc(i.bay_code)}</b>（${ctx.esc(i.bay_kind_label)}）
          · 查验员 <b>${ctx.esc(i.inspector_name || '未指派')}</b>
          ${i.required_qual_label ? ` · 需资质 <span class="qb ${i.required_qual}">${ctx.esc(i.required_qual_label)}</span>` : ''}
          <br/>报关单状态：${ctx.esc(i.decl_status_label)} · 查验状态：${ctx.esc(i.status_label)}
          ${i.overdue ? ' · <b style="color:var(--red-600)">已逾期未完成</b>' : ''}
          ${i.reassign_count ? ` · 历史改派/顺延 ${i.reassign_count} 次` : ''}
        </div>
        ${conflicts.length ? `<h3 style="margin:6px 0;font-size:13px">⚠️ 当前排期冲突（${conflicts.length}）</h3>
          ${conflictListHtml(conflicts, true)}` : '<div style="color:var(--green-600);font-size:12.5px;margin:6px 0">✅ 当前车位、查验员、资质与时间均无冲突。</div>'}
        <div class="small" style="margin-top:8px"><a id="insp-goto" style="color:var(--blue-500);cursor:pointer">查看报关单详情与生命周期留痕 →</a></div>`,
      footer: movable ? `
        <button class="btn" id="insp-cancel">取消排期</button>
        <button class="btn gold" id="insp-reassign">🔀 改派（车故障/人请假，链式顺延）</button>
        <button class="btn solid" id="insp-close">关闭</button>`
        : '<button class="btn solid" id="insp-close">关闭</button>',
    });
    bindConflictToggle(m.mask);
    m.mask.querySelector('#insp-goto').onclick = () => { m.close(); location.hash = `#/declarations/${i.declaration_id}`; };
    m.mask.querySelector('#insp-close').onclick = m.close;
    const cancelBtn = m.mask.querySelector('#insp-cancel');
    if (cancelBtn) cancelBtn.onclick = () => { m.close(); cancelInspection(i, y, view); };
    const raBtn = m.mask.querySelector('#insp-reassign');
    if (raBtn) raBtn.onclick = () => { m.close(); openReassign(i, y, view); };
  }

  async function cancelInspection(i, y, view) {
    const note = await ctx.confirmNote({
      title: `取消排期 · ${i.decl_no}`,
      message: `取消后该时间槽释放（车位/查验员可改排他单），报关单回到「等待重新安排查验」。此操作留痕。`,
      placeholder: '如：企业申请延期到场 / 台位不匹配需改场 / 报关资料撤回',
      danger: true, confirmText: '确认取消排期',
    });
    if (note === null) return;
    try {
      await Api.post(`/scheduling/inspections/${i.id}/cancel`, { reason: note });
      ctx.toast('排期已取消并留痕', 'warn');
      refresh(view);
    } catch (e) { ctx.toast(e.reason, 'error'); }
  }

  /* ================= 改派 + 链式重排 ================= */

  function openReassign(i, y, view, presetReason, onChanged) {
    const after = onChanged || (() => refresh(view));
    const bays = y.bays || [];
    const inspectors = state.resources.inspectors || [];
    const curBay = bays.find(b => b.id === i.bay_id);
    const dtVal = i.scheduled_at.slice(0, 16);
    const m = ctx.modal({
      title: `🔀 改派查验 · ${i.decl_no}`,
      wide: true,
      body: `
        <div class="warn-box">改派用于<b>车故障 / 人请假</b>：本单从当前车位/查验员解绑重派；
          同场站排在其后的单将按新占用<b>链式顺延</b>（只后移不前移、自动避开营业窗口），
          已放行 / 已结关 / 已完成的单<b>不动</b>，链条撞上锁定单会明确报出，不会形成环形依赖。</div>
        <div class="form-grid">
          <div class="full"><label>改派原因（必填，留痕）</label>
            <input id="ra-reason" value="${ctx.esc(presetReason || '')}" placeholder="如：A-12 升降平台液压故障停用，改派同类型台位"/></div>
          <div><label>新车位</label>
            <select id="ra-bay">${bays.map(b => `
              <option value="${b.id}" ${b.id === i.bay_id ? 'selected' : ''}
                ${b.out_of_service ? 'disabled' : ''}>${b.code} ${b.name}（${b.kind_label}）${b.out_of_service ? ' · 故障停用' : ''}</option>`).join('')}</select></div>
          <div><label>新查验员</label>
            <select id="ra-insp">${inspectors.map(u => `
              <option value="${u.id}" ${u.id === i.inspector_id ? 'selected' : ''}
                ${u.on_leave ? 'disabled' : ''}>${u.display_name.replace(/（.*?）/g, '')} ${(u.qual_labels || []).join('/')}${u.on_leave ? ' · 请假中' : ''}</option>`).join('')}</select></div>
          <div><label>新计划开始时间</label><input id="ra-time" type="datetime-local" value="${dtVal}"/></div>
          <div><label>&nbsp;</label><button class="btn sm" id="ra-preview">🔎 预演冲突与连锁影响</button></div>
        </div>
        <div id="ra-out" style="margin-top:12px"></div>`,
      footer: '<button class="btn" id="ra-cancel">取消</button><button class="btn solid" id="ra-ok" disabled>确认改派并顺延</button>',
    });
    let plan = null;
    m.mask.querySelector('#ra-cancel').onclick = m.close;

    async function dryRun() {
      const reason = m.mask.querySelector('#ra-reason').value.trim();
      if (reason.length < 4) { ctx.toast('请先填写不少于 4 字的改派原因', 'warn'); return; }
      const body = {
        reason,
        new_bay_id: Number(m.mask.querySelector('#ra-bay').value),
        new_inspector_id: Number(m.mask.querySelector('#ra-insp').value),
        new_scheduled_at: m.mask.querySelector('#ra-time').value,
        confirm: false,
      };
      const out = m.mask.querySelector('#ra-out');
      out.innerHTML = '<div class="small muted">正在校验新槽位并推演链式顺延…</div>';
      try {
        plan = await Api.post(`/scheduling/inspections/${i.id}/reassign`, body);
        renderPlan(out, plan);
        m.mask.querySelector('#ra-ok').disabled = false;
        m.mask.querySelector('#ra-ok').textContent =
          plan.need_confirm_window ? '确认改派（含超窗口，原因留痕）' : '确认改派并顺延';
        m._body = body;
      } catch (e) {
        plan = null;
        m.mask.querySelector('#ra-ok').disabled = true;
        if (e.code === 'scheduling_conflict') {
          out.innerHTML = `<div class="danger-box">目标槽位存在阻断冲突，改派无法执行，请更换车位/查验员/时间：</div>`
            + conflictListHtml(e.data.error.conflicts || [], false);
          bindConflictToggle(out);
        } else if (e.code === 'chain_blocked_by_locked') {
          out.innerHTML = `<div class="danger-box">⛔ ${ctx.esc(e.reason)}</div>`;
        } else {
          out.innerHTML = `<div class="danger-box">${ctx.esc(e.reason)}</div>`;
        }
      }
    }
    m.mask.querySelector('#ra-preview').onclick = dryRun;

    m.mask.querySelector('#ra-ok').onclick = async () => {
      if (!plan) return;
      const body = Object.assign({}, m._body, { confirm: true });
      try {
        const r = await Api.post(`/scheduling/inspections/${i.id}/reassign`, body);
        m.close();
        ctx.toast(`改派完成：本单已重派，${r.chain_moved_count} 单链式顺延（批次 ${r.chain_batch}）`, 'success');
        openChainResult(r, after);
        after();
      } catch (e) {
        if (e.code === 'reason_required') { ctx.toast(e.reason, 'warn'); }
        else ctx.toast(e.reason, 'error');
      }
    };
  }

  function renderPlan(out, plan) {
    const chain = plan.chain || [];
    const moved = chain.filter(c => c.inspection_id !== plan.target.id && c.moved);
    const target = chain.find(c => c.inspection_id === plan.target.id) || {};
    const barriers = plan.locked_barriers || [];
    out.innerHTML = `
      ${plan.conflicts && plan.conflicts.length ? conflictListHtml(plan.conflicts, true) : ''}
      ${plan.need_confirm_window
        ? '<div class="warn-box">🟡 本单新时间超出营业窗口，确认改派时原因将作为非常规安排留痕。</div>' : ''}
      <div class="small" style="font-weight:700;margin:6px 0">连锁影响预演（共 ${moved.length} 单需顺延）：</div>
      <div class="chain-box">
        <div class="chain-row target">
          <div>本单 <b class="mono">${ctx.esc(plan.target.decl_no)}</b><br/><span class="small muted">${ctx.fmtDate(target.old_start)}</span></div>
          <div class="arrow">➜</div>
          <div class="new">${ctx.fmtDate(target.new_start)}<br/><span class="small">${ctx.esc(plan.new_slot.bay)} · ${ctx.esc((plan.new_slot.inspector || '').replace(/（.*?）/g, ''))}</span></div>
        </div>
        ${moved.map(c => `
          <div class="chain-row">
            <div><b class="mono">${ctx.esc(c.decl_no)}</b> <span class="small muted">${ctx.esc(c.cargo_name)}</span><br/><span class="small muted">原 ${ctx.fmtDate(c.old_start)} · ${ctx.esc(c.bay_code)}</span></div>
            <div class="arrow">➜</div>
            <div class="new">顺延至 ${ctx.fmtDate(c.new_start)}<div class="small muted">${(c.reasons || []).slice(0, 2).map(r => ctx.esc(r)).join('；')}</div></div>
          </div>`).join('')
        || '<div class="small muted" style="padding:10px 12px">后续单不受影响，无需顺延。</div>'}
      </div>
      ${barriers.length ? `<div class="locked-box" style="margin-top:8px">🔒 以下已放行/结关/已完成或跨场站占用为<b>锁定屏障</b>，链条只绕开、绝不移动：
        ${barriers.slice(0, 5).map(b => `<span class="mono" style="margin-right:8px">${ctx.esc(b.decl_no)}（${ctx.fmtDate(b.scheduled_at)}）</span>`).join('')}
        ${barriers.length > 5 ? `等 ${barriers.length} 条` : ''}</div>` : ''}`;
    bindConflictToggle(out);
  }

  function openChainResult(r, onClose) {
    const moved = (r.chain || []).filter(c => c.inspection_id !== r.target.id && c.moved);
    ctx.modal({
      title: '✅ 改派与链式顺延已执行',
      wide: true,
      body: `
        <div style="background:var(--green-100);border:1px solid #a7dcc0;border-radius:8px;padding:10px 13px;font-size:13px;margin-bottom:10px">
          本单已重派到新车位/查验员；<b>${moved.length}</b> 张后续单按资源占用链式顺延，全部只后移不前移；
          已放行、已结关、已完成单未做任何改动。留痕批次：<span class="mono">${ctx.esc(r.chain_batch)}</span>
        </div>
        <div class="chain-box">
          <div class="chain-row target"><div>本单 <b class="mono">${ctx.esc(r.target.decl_no)}</b></div>
            <div class="arrow">➜</div><div class="new">${ctx.fmtDate(r.target.scheduled_at)} ${ctx.esc(r.target.bay_code)}</div></div>
          ${moved.map(c => `<div class="chain-row">
            <div class="mono">${ctx.esc(c.decl_no)}</div><div class="arrow">➜</div>
            <div class="new">${ctx.fmtDate(c.new_start)}</div></div>`).join('')}
        </div>`,
      footer: '<button class="btn solid" id="ok">完成</button>',
    }).mask.querySelector('#ok').onclick = onClose;
  }

  /* ================= 故障 / 请假 登记 ================= */

  async function toggleBay(bayId, setBroken, y, view) {
    const bay = y.bays.find(b => b.id === bayId);
    const label = setBroken ? '登记车位故障停用' : '恢复车位使用';
    let reason = '';
    if (setBroken) {
      reason = await ctx.confirmNote({
        title: `🅿️ ${label} · ${bay.code}`,
        message: '停用后该车位不能再排查验；已排在该车位的待执行任务将被列出，需逐单改派。',
        placeholder: '如：升降平台液压故障，预计 9/20 修复', danger: true, confirmText: '确认故障停用',
      });
      if (reason === null) return;
    }
    try {
      const r = await Api.post(`/scheduling/bays/${bayId}/status`, { out_of_service: setBroken, reason });
      ctx.toast(setBroken
        ? `车位已停用，${r.affected_inspection_ids.length} 条待执行任务需改派`
        : '车位已恢复使用', setBroken ? 'warn' : 'success');
      refresh(view);
    } catch (e) { ctx.toast(e.reason, 'error'); }
  }

  async function toggleInspector(uid, setLeave, y, view) {
    const u = state.resources.inspectors.find(x => x.id === uid);
    let reason = '';
    if (setLeave) {
      reason = await ctx.confirmNote({
        title: `🧑‍🔧 查验员请假 · ${u.display_name}`,
        message: '请假期间该查验员不能派单；已排给他的待执行任务将被列出，需逐单改派。',
        placeholder: '如：家中急事请事假 9/18–9/19，任务改派其他持证查验员', danger: true, confirmText: '确认请假',
      });
      if (reason === null) return;
    }
    try {
      const r = await Api.post(`/scheduling/inspectors/${uid}/status`, { on_leave: setLeave, reason });
      ctx.toast(setLeave
        ? `已登记请假，${r.affected_inspection_ids.length} 条待执行任务需改派`
        : '已销假，可以派单', setLeave ? 'warn' : 'success');
      state.resources = await Api.get('/scheduling/resources');
      refresh(view);
    } catch (e) { ctx.toast(e.reason, 'error'); }
  }

  /* ================= 安排查验（从报关单详情） ================= */

  async function openScheduleModal(decl, afterDone) {
    if (!state.resources) state.resources = await Api.get('/scheduling/resources');
    const yards = state.resources.yards || [];
    const inspectors0 = state.resources.inspectors || [];
    if (!yards.length || !yards.some(y => (y.bays || []).length) || !inspectors0.length) {
      ctx.toast('暂无可安排的监管场站/车位或查验员，请先维护资源', 'error');
      return;
    }
    const y0 = yards.find(y => (y.bays || []).length && decl.port && y.port === decl.port) || yards[0];
    const need = await Api.post('/scheduling/inspections/preview', {
      declaration_id: decl.id, yard_id: y0.id, bay_id: y0.bays[0].id,
      inspector_id: inspectors0[0].id,
      scheduled_at: defaultDt(), duration_minutes: 120,
    }).catch(() => ({ required_qual: null, required_qual_label: '', conflicts: [] }));

    const m = ctx.modal({
      title: `🛃 安排查验 · ${decl.decl_no}`,
      wide: true,
      body: `
        <div class="warn-box">海关逻辑审单已通过、决定布控查验。选择监管场站、匹配车位与持证查验员及时间；
          保存后报关单进入「查验中」。冲突会<b>逐条标红</b>，阻断冲突不可保存。</div>
        <div class="form-grid">
          <div><label>监管场站</label><select id="sh-yard">${yards.map(y =>
            `<option value="${y.id}" ${decl.port && y.port === decl.port ? 'selected' : ''}>${y.name}（${y.port}，${fmtWindow(y)}）</option>`).join('')}</select></div>
          <div><label>查验车位</label><select id="sh-bay"></select></div>
          <div><label>查验员</label><select id="sh-insp"></select></div>
          <div><label>计划开始</label><input id="sh-time" type="datetime-local" value="${defaultDt()}"/></div>
          <div><label>查验时长（分钟）</label><input id="sh-dur" type="number" step="30" value="120" min="30" max="480"/></div>
        </div>
        ${need.required_qual_label ? `<div class="small" style="margin-top:8px">该货需查验资质：
          <span class="qb ${need.required_qual}">${ctx.esc(need.required_qual_label)}</span>
          车位类型与查验员资质必须匹配。</div>` : '<div class="small muted" style="margin-top:8px">该货为普通货物，普货台位与任意查验员均可。</div>'}
        <div id="sh-conflicts" style="margin-top:10px"></div>
        <div id="sh-confirm-wrap" class="hidden">
          <label style="margin-top:8px">非常规安排原因（不少于 4 字，留痕）</label>
          <textarea id="sh-reason" rows="2" placeholder="超营业窗口落位原因"></textarea>
        </div>`,
      footer: '<button class="btn" id="sh-cancel">取消</button><button class="btn solid" id="sh-ok">安排查验并布控</button>',
    });

    function defaultDt() { return toIsoMin(roundHalf(new Date(Date.now() + 3600000))); }
    const $ = sel => m.mask.querySelector(sel);
    function curYard() { return yards.find(y => y.id === Number($('#sh-yard').value)); }
    function fillBays() {
      const y = curYard();
      $('#sh-bay').innerHTML = y.bays.map(b =>
        `<option value="${b.id}" ${b.out_of_service ? 'disabled' : ''}>${b.code} ${b.name}（${b.kind_label}）${b.out_of_service ? ' · 故障' : ''}</option>`).join('');
    }
    function fillInsps() {
      $('#sh-insp').innerHTML = state.resources.inspectors.map(u =>
        `<option value="${u.id}" ${u.on_leave ? 'disabled' : ''}>${u.display_name.replace(/（.*?）/g, '')} ${(u.qual_labels || []).join('/') || '普货'}${u.on_leave ? ' · 请假' : ''}</option>`).join('');
    }
    fillBays(); fillInsps();
    $('#sh-yard').onchange = () => { fillBays(); fillInsps(); livePreview(); };
    $('#sh-bay').onchange = livePreview;
    $('#sh-insp').onchange = livePreview;
    $('#sh-time').onchange = livePreview;
    $('#sh-dur').onchange = livePreview;

    let latest = { blocking: [], need_confirm: false };
    async function livePreview() {
      const body = collect(false);
      try {
        const r = await Api.post('/scheduling/inspections/preview', body);
        latest = r;
        const box = $('#sh-conflicts');
        box.innerHTML = (r.conflicts || []).length
          ? conflictListHtml(r.conflicts, false)
          : '<div style="color:var(--green-600);font-size:12.5px">✅ 该时间槽无冲突。</div>';
        bindConflictToggle(box);
        $('#sh-confirm-wrap').classList.toggle('hidden', !(r.need_confirm));
        $('#sh-ok').disabled = (r.blocking || []).length > 0;
        $('#sh-ok').textContent = r.need_confirm ? '超窗口安排（二次确认）' : '安排查验并布控';
      } catch (e) { /* 预览失败不阻塞填写，提交时服务端再校验 */ }
    }
    function collect(confirm) {
      return {
        declaration_id: decl.id,
        yard_id: Number($('#sh-yard').value),
        bay_id: Number($('#sh-bay').value),
        inspector_id: Number($('#sh-insp').value),
        scheduled_at: $('#sh-time').value,
        duration_minutes: Number($('#sh-dur').value || 120),
        reason: $('#sh-reason') ? $('#sh-reason').value.trim() : '',
        confirm,
      };
    }
    $('#sh-cancel').onclick = m.close;
    $('#sh-ok').onclick = async () => {
      const needConfirm = latest.need_confirm;
      if (needConfirm && collect(true).reason.length < 4) { ctx.toast('超窗口落位须填写不少于 4 字原因', 'warn'); return; }
      try {
        const r = await Api.post('/scheduling/inspections', collect(needConfirm));
        m.close();
        ctx.toast(`已安排查验（${r.inspection.yard_name} ${r.inspection.bay_code}），报关单进入查验中`, 'success');
        afterDone && afterDone();
      } catch (e) {
        if (e.code === 'need_confirm') { livePreview(); ctx.toast('请填写超窗口原因后再次确认', 'warn'); }
        else if (e.code === 'scheduling_conflict') {
          openConflictModal('安排查验被冲突拦下', e.data.error.conflicts || [], null, curYard());
        } else ctx.toast(e.reason, 'error');
      }
    };
    setTimeout(livePreview, 50);
  }

  /* ================= 及时率（双口径） ================= */

  async function renderTimeliness(el0, view) {
    el0.innerHTML = '<div class="small muted">加载及时率…</div>';
    try {
      const r = await Api.get(`/scheduling/timeliness?month=${state.month}`);
      const c = r.current || {};
      const d = r.definitions;
      const diverge = c.day_rate != null && c.completion_rate != null &&
        Math.abs(c.day_rate - c.completion_rate) >= 20;
      el0.innerHTML = `
        <div class="sched-toolbar" style="margin-bottom:10px">
          <label class="small">统计月份</label>
          <input type="month" id="sc-month" value="${state.month}" style="width:auto"/>
          ${diverge ? '<span class="dot amber">本月两口径背离，重点关注</span>' : ''}
        </div>
        <div class="kpi-duo">
          <div class="kpi-caliber ${diverge ? 'diverge' : ''}">
            <div class="cal-name">📅 ${ctx.esc(d.day_rate.name)}</div>
            <div class="cal-num">${c.day_rate == null ? '—' : c.day_rate}<small>%</small></div>
            <div class="cal-frac">准时 ${c.on_time_count ?? '—'} / 应查验 ${c.due_count ?? '—'} 单</div>
            <div class="caliber-detail" id="cal-a">
              <b>公式：</b>${ctx.esc(d.day_rate.formula)}<br/>
              <b>分母：</b>${ctx.esc(d.day_rate.denominator)}<br/>
              <b>分子：</b>${ctx.esc(d.day_rate.numerator)}<br/>
              <b>回答：</b>${ctx.esc(d.day_rate.answers)}
            </div>
          </div>
          <div class="kpi-caliber ${diverge ? 'diverge' : ''}">
            <div class="cal-name">✅ ${ctx.esc(d.completion_rate.name)}</div>
            <div class="cal-num">${c.completion_rate == null ? '—' : c.completion_rate}<small>%</small></div>
            <div class="cal-frac">已完成 ${c.completed ?? '—'} / 派单 ${c.dispatched ?? '—'} 单（待查/异常 ${c.pending_or_abnormal ?? '—'}）</div>
            <div class="caliber-detail" id="cal-b">
              <b>公式：</b>${ctx.esc(d.completion_rate.formula)}<br/>
              <b>分母：</b>${ctx.esc(d.completion_rate.denominator)}<br/>
              <b>分子：</b>${ctx.esc(d.completion_rate.numerator)}<br/>
              <b>回答：</b>${ctx.esc(d.completion_rate.answers)}
            </div>
          </div>
        </div>
        <div class="warn-box" style="margin-top:10px">📊 ${ctx.esc(d.divergence_note)}</div>
        <div class="table-wrap"><table class="trend-table">
          <thead><tr><th>月份</th><th>口径一 · 准点率</th><th>口径二 · 完成率</th><th>应查/准时</th><th>派单/完成</th><th>改派顺延次数</th></tr></thead>
          <tbody>${r.series.map(s => {
            const dv = s.day_rate != null && s.completion_rate != null && Math.abs(s.day_rate - s.completion_rate) >= 20;
            return `<tr ${s.month === state.month ? 'style="background:var(--blue-100)"' : ''}>
              <td>${s.month}</td>
              <td class="${dv ? 'diverge' : ''}">${s.day_rate == null ? '—' : s.day_rate + '%'}</td>
              <td class="${dv ? 'diverge' : ''}">${s.completion_rate == null ? '—' : s.completion_rate + '%'}</td>
              <td>${s.due_count}/${s.on_time_count}</td>
              <td>${s.dispatched}/${s.completed}</td>
              <td>${s.reassign_total}</td></tr>`;
          }).join('')}</tbody>
        </table></div>
        <div class="small muted">点击两张口径卡片可展开/收起完整口径定义。</div>`;
      el0.querySelector('#sc-month').onchange = e => { state.month = e.target.value; renderTimeliness(el0, view); };
      el0.querySelectorAll('.kpi-caliber').forEach(card => card.onclick = () => {
        card.querySelector('.caliber-detail').classList.toggle('open');
      });
    } catch (e) {
      el0.innerHTML = `<div class="danger-box">及时率加载失败：${ctx.esc(e.reason)}</div>`;
    }
  }

  /* ================= 排期留痕 ================= */

  async function openChanges(y) {
    let rows = [];
    try { rows = await Api.get(`/scheduling/changes?yard_id=${y.id}&limit=80`); }
    catch (e) { ctx.toast(e.reason, 'error'); return; }
    ctx.modal({
      title: `📜 ${y.name} · 排期变更留痕`,
      wide: true,
      body: rows.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>时间</th><th>类型</th><th>操作人</th><th>原因</th><th>批次/详情</th></tr></thead>
        <tbody>${rows.map(r => `
          <tr><td class="small">${ctx.fmtDate(r.created_at)}</td>
            <td><span class="pill filing">${ctx.esc(r.change_type_label)}</span></td>
            <td>${ctx.esc(r.actor_name)}</td>
            <td class="small">${ctx.esc(r.reason)}</td>
            <td class="small muted">${r.chain_batch ? '批次 ' + ctx.esc(r.chain_batch) : ''}
              <div class="small" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                title="${ctx.esc(JSON.stringify(r.detail))}">${ctx.esc(JSON.stringify(r.detail))}</div></td>
          </tr>`).join('')}</tbody></table></div>`
        : '<div class="empty-state"><div class="ico">📭</div><h3>该场站暂无排期变更记录</h3></div>',
      footer: '<button class="btn solid" id="ok">关闭</button>',
    }).mask.querySelector('#ok').onclick = () => {};
  }

  /* ================= 报关单详情：查验卡 + 安排按钮 ================= */

  function bindDetail(view, data) {
    const slot = view.querySelector('#detail-inspection');
    if (!slot) return;
    const d = data.declaration;
    const all = data.inspections || [];
    const active = all.filter(i => i.status !== 'cancelled');
    const cancelled = all.filter(i => i.status === 'cancelled');
    const canSchedule = ['customs', 'supervisor'].includes(Api.user.role)
      && ['reviewing', 'inspecting'].includes(d.status);

    let html = '';
    if (active.length) {
      const i = active[active.length - 1];
      html = `
        <div class="kv" style="margin-bottom:8px">
          <dt>当前排期</dt><dd><b>${ctx.fmtDate(i.scheduled_at)}</b> – ${hm(parseDt(i.scheduled_end))}</dd>
          <dt>场站车位</dt><dd>${ctx.esc(i.yard_name || '')} · ${ctx.esc(i.bay_code)}（${ctx.esc(i.bay_kind_label)}）
            ${i.required_qual_label ? ` <span class="qb ${i.required_qual}">${ctx.esc(i.required_qual_label)}</span>` : ''}</dd>
          <dt>查验员</dt><dd>${ctx.esc(i.inspector_name || '未指派')}</dd>
          <dt>应查验日</dt><dd>${i.due_date ? ctx.fmtDate(i.due_date, false) : '—'}
            <span class="small muted">（及时率准点口径基准，改派不回改）</span></dd>
          <dt>状态</dt><dd>${ctx.esc(i.status_label)} ${i.reassign_count ? `· 改派/顺延 ${i.reassign_count} 次` : ''}</dd>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn sm" id="di-gantt">🗓️ 甘特图查看/改期</button>
          ${canSchedule ? '<button class="btn sm gold" id="di-reassign">🔀 改派</button>' : ''}
          ${canSchedule && i.status === 'pending' ? '<button class="btn sm" id="di-cancel">取消排期</button>' : ''}
        </div>`;
    } else if (cancelled.length) {
      // —— 该单空态：排期全部取消（区别于从未安排）——
      html = `
        <div class="state-block cancelled" style="padding:20px 12px">
          <div class="ico" style="font-size:34px">🚫</div>
          <h3 style="font-size:14px">该单查验排期已全部取消（${cancelled.length} 次）</h3>
          <div class="state-desc" style="font-size:12px">
            ${cancelled.slice().reverse().map(c =>
              `<div style="text-align:left;background:#fff;border-radius:6px;padding:6px 10px;margin-top:6px;border:1px solid #ecd3a0">
                原计划 ${ctx.fmtDate(c.scheduled_at)} · ${ctx.esc(c.bay_code)} · ${ctx.esc(c.inspector_name || '')}<br/>
                <b>取消原因：</b>${ctx.esc(c.cancel_reason || '未填写')}</div>`).join('')}
          </div>
          ${canSchedule ? '<button class="btn sm solid" id="di-schedule" style="margin-top:8px">重新安排查验</button>' : ''}
        </div>`;
    } else {
      // —— 该单空态：从未安排排期 ——
      html = `
        <div class="state-block" style="padding:22px 12px">
          <div class="ico" style="font-size:34px">🗓️</div>
          <h3 style="font-size:14px">该单尚无查验排期</h3>
          <div class="state-desc" style="font-size:12px">
            ${d.status === 'reviewing' ? '海关逻辑审核通过并布控后，可在此安排查验车位、查验员与时间。'
              : d.status === 'inspecting' ? '单据已在查验环节但还没有有效排期，请安排查验。'
              : '当前环节无需查验；只有审单布控后才会生成查验排期。'}
          </div>
          ${canSchedule ? '<button class="btn sm solid" id="di-schedule">安排查验</button>' : ''}
        </div>`;
    }
    slot.innerHTML = html;

    const goto = () => { location.hash = '#/scheduling'; };
    const g = slot.querySelector('#di-gantt'); if (g) g.onclick = goto;
    const sBtn = slot.querySelector('#di-schedule');
    if (sBtn) sBtn.onclick = () => openScheduleModal(d, () => ctx.rerun());
    const rBtn = slot.querySelector('#di-reassign');
    if (rBtn) {
      rBtn.onclick = async () => {
        const iCur = active[active.length - 1];
        if (!state.resources) state.resources = await Api.get('/scheduling/resources');
        const yy = state.resources.yards.find(x => x.id === iCur.yard_id);
        if (!yy) { ctx.toast('该排期所属场站资源未找到', 'error'); return; }
        // view 传 null：改派完成后整页刷新详情，不依赖甘特视图
        openReassign(iCur, yy, null, null, () => ctx.rerun());
      };
    }
    const cBtn = slot.querySelector('#di-cancel');
    if (cBtn) cBtn.onclick = async () => {
      const i = active[active.length - 1];
      const note = await ctx.confirmNote({
        title: '取消查验排期', message: '取消后释放车位与查验员，需重新安排。', danger: true,
        confirmText: '确认取消', placeholder: '请填写取消原因（不少于 4 字）',
      });
      if (note === null) return;
      try { await Api.post(`/scheduling/inspections/${i.id}/cancel`, { reason: note }); ctx.toast('已取消', 'warn'); ctx.rerun(); }
      catch (e) { ctx.toast(e.reason, 'error'); }
    };
  }
})();
