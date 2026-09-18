/* 报关通前端主应用：登录壳 + 作战台 + 报关单 + 客户与委托（hash 路由，零构建）。 */
(function () {
  'use strict';

  const $app = document.getElementById('app');
  const $banner = document.getElementById('net-banner');

  /* ---------------- 通用工具 ---------------- */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fmtMoney(v) {
    return '¥' + Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtDate(iso, withTime = true) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
      (withTime ? ` ${p(d.getHours())}:${p(d.getMinutes())}` : '');
  }
  function el(html) {
    const tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    return tpl.content.firstElementChild;
  }
  function toast(msg, type = '') {
    const root = document.getElementById('toast-root');
    const t = el(`<div class="toast ${type}">${esc(msg)}</div>`);
    root.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, 4200);
  }

  /* ---------------- 弹窗 ---------------- */

  function modal({ title, body, footer, wide }) {
    const root = document.getElementById('modal-root');
    const mask = el(`
      <div class="modal-mask">
        <div class="modal ${wide ? 'wide' : ''}">
          <div class="modal-head"><h3>${esc(title)}</h3><span class="x">✕</span></div>
          <div class="modal-body"></div>
          <div class="modal-foot" style="display:${footer === false ? 'none' : 'flex'}"></div>
        </div>
      </div>`);
    const bodyEl = mask.querySelector('.modal-body');
    const footEl = mask.querySelector('.modal-foot');
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (body) bodyEl.appendChild(body);
    if (footer !== false) {
      if (typeof footer === 'string') footEl.innerHTML = footer;
      else if (footer) footEl.appendChild(footer);
    }
    const close = () => mask.remove();
    mask.addEventListener('click', e => { if (e.target === mask) close(); });
    mask.querySelector('.x').addEventListener('click', close);
    root.appendChild(mask);
    return { mask, bodyEl, footEl, close };
  }

  function confirmNote({ title, message, placeholder, confirmText = '确认执行', danger }) {
    return new Promise(resolve => {
      const m = modal({
        title,
        body: `
          ${message ? `<div class="${danger ? 'danger-box' : 'warn-box'}">${esc(message)}</div>` : ''}
          <label>处理说明（将记入生命周期留痕）</label>
          <textarea id="m-note" rows="3" placeholder="${esc(placeholder || '请填写本次操作的依据/说明')}"></textarea>`,
        footer: `
          <button class="btn" id="m-cancel">取消</button>
          <button class="btn solid ${danger ? 'danger' : ''}" id="m-ok" style="${danger ? 'background:var(--red-600);border-color:var(--red-600)' : ''}">${esc(confirmText)}</button>`,
      });
      m.mask.querySelector('#m-cancel').onclick = () => { m.close(); resolve(null); };
      m.mask.querySelector('#m-ok').onclick = () => {
        const note = m.mask.querySelector('#m-note').value.trim();
        if (note.length < 2) { toast('请填写处理说明后再提交', 'warn'); return; }
        m.close(); resolve(note);
      };
    });
  }

  /* ---------------- 网络横幅 ---------------- */

  function renderBanner(online) {
    const pending = Offline.pendingCount();
    if (!online) {
      $banner.className = 'net-banner offline';
      $banner.innerHTML = `⚠️ 当前处于<span>离线模式</span>（口岸现场无网络）—— 页面数据为缓存快照、非实时状态；`
        + `可离线起草报关单/排队流转，恢复网络后自动合并，不会重复建单。待同步 ${pending} 项。`;
    } else if (pending > 0) {
      $banner.className = 'net-banner online-recovered';
      $banner.innerHTML = `🔄 网络已恢复，正在同步 ${pending} 项离线数据（幂等合并）…`;
    } else {
      $banner.className = 'net-banner hidden';
      $banner.innerHTML = '';
    }
  }

  /* ---------------- 路由 ---------------- */

  const routes = {};
  function route(path, handler) { routes[path] = handler; }

  async function router() {
    const rawHash = (location.hash.replace(/^#/, '') || '/dashboard');
    const pathOnly = rawHash.split('?')[0];
    const [path, ...rest] = pathOnly.split('/').filter(Boolean);
    const key = '/' + (path || 'dashboard');
    const handler = routes[key] || routes['/dashboard'];
    if (!Api.loggedIn) {
      if (location.hash !== '#/login') location.hash = '#/login';
      // 直接渲染登录页，不依赖 hashchange 是否异步触发（兼容弱网络/内嵌 WebView）
      return routes['/login']();
    }
    try {
      await handler(rest);
    } catch (e) {
      renderError(e);
    }
  }

  function renderError(e) {
    const reason = e && e.reason ? e.reason : '页面加载失败，请稍后重试。';
    const code = e && e.code ? e.code : 'unknown';
    const isAuth = e.status === 401;
    const isForbidden = e.status === 403;
    if (isAuth) {
      Api.logoutLocal();
      location.hash = '#/login';
      return;
    }
    $app.innerHTML = '';
    const box = el(`
      <div style="padding:40px;max-width:720px;margin:0 auto">
        <div class="error-state">
          <div class="ico">${isForbidden ? '🚫' : '⚠️'}</div>
          <h3>${isForbidden ? '访问被合规策略拦截' : '出错了'}</h3>
          <div class="reason"><b>错误码：</b>${esc(code)}<br/><b>原因：</b>${esc(reason)}</div>
          <button class="btn solid" id="e-back">返回作战台</button>
        </div>
      </div>`);
    box.querySelector('#e-back').onclick = () => { location.hash = '#/dashboard'; };
    $app.appendChild(box);
  }

  /* ---------------- 登录 ---------------- */

  const DEMO = [
    ['zhang_wei', '张伟 · 报关员（盐田/蛇口）'],
    ['wang_hua', '王华 · 海关审单员'],
    ['admin_huateng', '陈志成 · 华腾企业管理员'],
    ['zhao_jing', '赵静 · 监管员'],
  ];

  route('/login', async () => {
    $app.innerHTML = `
      <div class="login-wrap">
        <div class="login-card">
          <div class="login-logo">🛃</div>
          <h1>报关通</h1>
          <div class="sub">报关行自研 · 进出口报关单全生命周期与合规风控平台</div>
          <div class="login-error" id="lg-err"></div>
          <div class="field"><label>账号</label><input id="lg-user" placeholder="如 zhang_wei" autocomplete="username"/></div>
          <div class="field"><label>口令</label><input id="lg-pass" type="password" placeholder="演示口令 bgt123456" autocomplete="current-password"/></div>
          <button class="btn-primary" id="lg-btn">登 录</button>
          <div class="demo-accounts">
            <h3>预置账号（点击自动填充，统一口令 bgt123456）</h3>
            <table>
              ${DEMO.map(([u, n]) => `<tr><td class="u" data-u="${u}">${u}</td><td class="muted">${n}</td></tr>`).join('')}
            </table>
          </div>
        </div>
      </div>`;
    const $u = document.getElementById('lg-user');
    const $p = document.getElementById('lg-pass');
    const $err = document.getElementById('lg-err');
    document.querySelectorAll('.demo-accounts .u').forEach(td => {
      td.onclick = () => { $u.value = td.dataset.u; $p.value = 'bgt123456'; $p.focus(); };
    });
    async function doLogin() {
      if (!navigator.onLine) { toast('当前完全离线，无法登录，请先接入网络完成身份认证。', 'error'); return; }
      try {
        const r = await Api.post('/auth/login', { username: $u.value.trim(), password: $p.value });
        Api.setToken(r.token);
        Api.setUser(r.user);
        toast(`欢迎，${r.user.display_name}`, 'success');
        location.hash = '#/dashboard';
      } catch (e) {
        $err.textContent = e.reason || '登录失败';
        $err.classList.add('show');
      }
    }
    document.getElementById('lg-btn').onclick = doLogin;
    $p.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  });

  /* ---------------- 主框架 ---------------- */

  const NAV = [
    { path: '/dashboard', icon: '🎯', label: '报关作战台' },
    { path: '/scheduling', icon: '🗓️', label: '查验排期' },
    { path: '/declarations', icon: '📄', label: '报关单' },
    { path: '/clients', icon: '🤝', label: '客户与委托' },
    { path: '/audit', icon: '🛡️', label: '全名查看留痕' },
  ];

  function shell(active) {
    const u = Api.user;
    $app.innerHTML = `
      <div class="layout">
        <aside class="sidebar">
          <div class="brand">
            <span class="logo">🛃</span>
            <div><div class="name">报关通</div><div class="tag">BAOGUAN TONG</div></div>
          </div>
          <nav class="nav">
            ${NAV.map(n => `
              <div class="nav-item ${active === n.path ? 'active' : ''}" data-go="${n.path}">
                <span class="icon">${n.icon}</span><span>${n.label}</span>
                ${n.path === '/dashboard' ? '<span class="nav-badge hidden" id="nav-red">0</span>' : ''}
              </div>`).join('')}
          </nav>
          <div class="sidebar-foot">
            <div class="user-chip">
              <div class="avatar">${esc(u.display_name.slice(0, 1))}</div>
              <div style="min-width:0">
                <div class="uname">${esc(u.display_name)}</div>
                <div class="urole">${esc(u.role_label)}${u.port ? ' · ' + esc(u.port) : ''}</div>
              </div>
            </div>
            <span class="logout-link" id="logout">退出登录</span>
          </div>
        </aside>
        <main class="main" id="view"></main>
      </div>`;
    document.querySelectorAll('.nav-item').forEach(n => {
      n.onclick = () => { location.hash = '#' + n.dataset.go; };
    });
    document.getElementById('logout').onclick = async () => {
      try { await Api.post('/auth/logout', {}); } catch {}
      Api.logoutLocal();
      location.hash = '#/login';
    };
    return document.getElementById('view');
  }

  function setNavBadge(n) {
    const b = document.getElementById('nav-red');
    if (!b) return;
    if (n > 0) { b.textContent = n; b.classList.remove('hidden'); } else b.classList.add('hidden');
  }

  const STATUS_CLASS = {
    entrusted: 's-entrusted', entered: 's-entered', reviewing: 's-reviewing',
    inspecting: 's-inspecting', released: 's-released', closed: 's-closed', cancelled: 's-cancelled',
  };
  function statusTag(s, label) {
    return `<span class="tag-status ${STATUS_CLASS[s] || ''}">${esc(label || s)}</span>`;
  }
  function offlineStamp(savedAt) {
    return savedAt ? `<span class="stale-stamp" title="离线快照，非实时状态">🕓 离线缓存 · 截至 ${fmtDate(savedAt)}</span>` : '';
  }

  /* ================= 作战台 ================= */

  route('/dashboard', async () => {
    const view = shell('/dashboard');
    view.innerHTML = loading();
    let data, staleAt = null;
    try {
      data = await Api.get('/dashboard/overview');
      Offline.saveSnapshot('dashboard', data);
    } catch (e) {
      if (e.code === 'network_offline') {
        const snap = Offline.getSnapshot('dashboard');
        if (!snap) throw e;
        data = snap.data; staleAt = snap.saved_at;
        toast('离线模式：作战台展示缓存快照（非实时态势），操作将离线排队', 'warn');
      } else throw e;
    }
    renderDashboard(view, data, staleAt);
    const pending = Offline.pendingCount();
    setNavBadge(data.red_dots.tax_overdue + data.red_dots.inspection_overdue);
    if (pending > 0 && navigator.onLine) triggerFlush();
  });

  function renderDashboard(view, d, staleAt) {
    const rd = d.red_dots;
    const role = Api.user.role;
    const canSeeAudit = role !== 'enterprise';

    view.innerHTML = `
      <div class="page-head">
        <div>
          <h2>🎯 报关作战台</h2>
          <div class="desc">各企业待审漏斗 · 查验排期 · 征税与逾期红点
            ${staleAt ? offlineStamp(staleAt) : `<span class="muted small">（数据更新于 ${fmtDate(d.generated_at)}）</span>`}</div>
        </div>
        <button class="btn solid" id="refresh">🔄 刷新态势</button>
      </div>

      <div class="stat-row">
        <div class="stat ${rd.tax_overdue ? 'alert' : 'ok'}">
          <div class="k">🚨 税款逾期未缴</div>
          <div class="v">${rd.tax_overdue} <span class="small">单</span></div>
          <div class="sub2">超过缴款期限，立即催办</div>
        </div>
        <div class="stat ${rd.tax_due_soon ? 'warn' : ''}">
          <div class="k">⏳ ${d.tax_warning_days}日内到期税款</div>
          <div class="v">${rd.tax_due_soon} <span class="small">单</span></div>
          <div class="sub2">在途未缴共 ${rd.tax_unpaid} 单</div>
        </div>
        <div class="stat ${rd.inspection_overdue ? 'alert' : ''}">
          <div class="k">📦 查验排期 / 逾期</div>
          <div class="v">${rd.inspection_today}<span class="small"> 今日</span> / ${rd.inspection_overdue}<span class="small"> 逾期</span></div>
          <div class="sub2">布控任务共 ${d.inspection_schedule.length} 条</div>
        </div>
        <div class="stat">
          <div class="k">📊 在途报关单总量</div>
          <div class="v">${d.totals_funnel.reduce((s, x) => s + (x.status === 'closed' ? 0 : x.count), 0)}</div>
          <div class="sub2">已结关 ${d.totals_funnel.find(x => x.status === 'closed')?.count || 0} 单</div>
        </div>
      </div>

      <div class="card">
        <h3>📈 全行待审漏斗（六态）</h3>
        <div class="funnel-wrap">${totalFunnel(d.totals_funnel)}</div>
      </div>

      <div class="section-title">🏢 各企业作战面板</div>
      <div class="ent-grid">
        ${d.enterprises.map(entCard).join('') || emptyInline('暂无企业数据')}
      </div>

      <div class="card" style="margin-top:16px">
        <h3>📅 查验排期 ${rd.inspection_overdue ? `<span class="dot red">逾期 ${rd.inspection_overdue}</span>` : ''}</h3>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>计划时间</th><th>状态</th><th>报关单号</th><th>企业</th><th>货物</th><th>场站 / 车位</th><th>结果备注</th></tr></thead>
            <tbody>
              ${d.inspection_schedule.map(i => `
                <tr class="clickable" data-decl="${i.declaration_id}">
                  <td class="${i.overdue ? 'tax-line overdue' : ''}">${fmtDate(i.scheduled_at)} ${i.overdue ? '🔴已逾期' : ''}</td>
                  <td><span class="dot ${i.status === 'done' ? 'green' : i.status === 'abnormal' ? 'red' : i.status === 'cancelled' ? 'gray' : 'amber'}">${esc(i.status_label)}</span></td>
                  <td class="mono">${esc(i.decl_no)}</td>
                  <td>${esc(i.enterprise_name)}</td>
                  <td>${esc(i.cargo_name)}</td>
                  <td>${esc(i.yard_name || i.port || '')} / ${esc(i.bay_code || '')}</td>
                  <td class="muted small">${esc(i.result_note || (i.status === 'cancelled' ? ('已取消：' + (i.cancel_reason || '')) : '—'))}</td>
                </tr>`).join('') || '<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">暂无查验任务</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>`;

    view.querySelector('#refresh').onclick = () => router();
    view.querySelectorAll('[data-decl]').forEach(tr => {
      tr.onclick = () => { location.hash = `#/declarations/${tr.dataset.decl}`; };
    });
    view.querySelectorAll('[data-ent-decls]').forEach(b => {
      b.onclick = () => { location.hash = `#/declarations?ent=${b.dataset.entDecls}`; };
    });
    view.querySelectorAll('[data-decl-id]').forEach(b => {
      b.onclick = () => { location.hash = `#/declarations/${b.dataset.declId}`; };
    });
  }

  function totalFunnel(stages) {
    const max = Math.max(1, ...stages.map(s => s.count));
    return `<div class="funnel">` + stages.map((s, i) => `
      <div class="funnel-stage">
        <div class="funnel-bar" style="height:${30 + (s.count / max) * 64}px">${s.count}</div>
        <div class="funnel-label">${esc(s.label)}</div>
      </div>${i < stages.length - 1 ? '<span class="funnel-arrow">→</span>' : ''}`).join('') + `</div>`;
  }

  function entCard(e) {
    const colors = ['#2f6fb3', '#6a4cc0', '#c9952c', '#1e8a55', '#b9791a'];
    const seg = e.funnel.map((f, i) =>
      `<div class="seg" style="background:${colors[i % colors.length]}" title="${esc(f.label)}：${f.count}">${f.count ? f.count : ''}</div>`).join('');
    const taxAlerts = e.tax_items.filter(t => t.flag === 'overdue' || t.flag === 'due_soon');
    return `
      <div class="ent-card">
        <div class="ec-head" data-ent-decls="${e.enterprise_id}" title="点击查看该企业全部报关单">
          <span>🏢</span>
          <div><div class="ec-name">${esc(e.enterprise_name)}</div><div class="ec-code mono">${esc(e.code)} · ${esc(e.ent_status === 'active' ? '已备案' : e.ent_status)}</div></div>
          <span style="margin-left:auto;font-size:12px;opacity:.7">在途 ${e.active_declarations} ▸</span>
        </div>
        <div class="ec-body">
          <div class="ec-kpis">
            <div class="ec-kpi ${e.pending_review_count ? 'alert' : ''}"><b>${e.pending_review_count}</b>待审/审单中</div>
            <div class="ec-kpi ${e.tax_overdue_count ? 'alert' : ''}"><b>${e.tax_overdue_count}</b>税逾期</div>
            <div class="ec-kpi ${e.tax_due_soon_count ? 'alert' : ''}"><b>${e.tax_due_soon_count}</b>税临期</div>
            <div class="ec-kpi"><b>${e.cancelled}</b>已撤销</div>
          </div>
          <div class="mini-funnel">${seg}</div>
          <div class="small muted" style="display:flex;gap:4px;flex-wrap:wrap">
            ${e.funnel.map((f, i) => `<span style="display:inline-flex;align-items:center;gap:3px;margin-right:8px">
              <i style="width:8px;height:8px;border-radius:2px;background:${colors[i % colors.length]};display:inline-block"></i>${esc(f.label)}</span>`).join('')}
          </div>
          ${taxAlerts.length ? `<div style="margin-top:8px">
            ${taxAlerts.slice(0, 3).map(t => `
              <div class="tax-line ${t.flag}">
                ${t.flag === 'overdue' ? '🔴' : '🟡'}
                <span class="mono">${esc(t.decl_no)}</span>
                <span class="amt">${fmtMoney(t.tax_amount)}</span>
                <span>${t.flag === 'overdue' ? `已逾期 ${Math.round(-t.days_left)} 天` : `还剩 ${t.days_left} 天`}</span>
                <span class="go" data-decl-id="${t.decl_id}">去处置 ▸</span>
              </div>`).join('')}
          </div>` : '<div class="small muted" style="margin-top:8px">✅ 暂无征税逾期/临期告警</div>'}
        </div>
      </div>`;
  }

  function emptyInline(msg) { return `<div class="empty-state" style="padding:30px"><div class="ico">📭</div><h3>${esc(msg)}</h3></div>`; }
  function loading() { return '<div class="empty-state"><div class="ico">⏳</div><h3>加载中…</h3></div>'; }

  /* ================= 报关单列表 ================= */

  let listCache = { enterprises: [] };

  function renderDeclList(view, list, ents, q, staleAt) {
    const isBroker = Api.user.role === 'broker';
    const drafts = Offline.listDrafts();
    view.innerHTML = `
      <div class="page-head">
        <div><h2>📄 报关单</h2>
          <div class="desc">全生命周期单据 ${staleAt ? offlineStamp(staleAt) : ''}（共 ${list.length} 张）</div></div>
        ${isBroker ? '<button class="btn solid" id="new-decl">＋ 新立项 / 离线起草</button>' : ''}
      </div>

      ${(!navigator.onLine || drafts.length) ? `
      <div class="offline-panel">
        <div class="op-title">📱 口岸离线起草箱（本地保存，恢复网络后自动幂等补传，不会重复建单）</div>
        ${drafts.length ? drafts.map(d => `
          <div class="offline-queue-item">
            <span>📝</span><span class="mono">${esc(d.client_ref)}</span>
            <span>${esc(d.cargo_name)} · ${esc(d.port)}</span>
            <span class="muted small">${fmtDate(d.queued_at)}</span>
            <span class="dot ${navigator.onLine ? 'green' : 'amber'}">${navigator.onLine ? '待同步' : '离线中'}</span>
            <button class="btn sm" data-del-draft="${esc(d.client_ref)}">移除</button>
          </div>`).join('') : '<div class="small muted">当前没有离线草稿。断网时可点击右上角按钮先起草。</div>'}
      </div>` : ''}

      <div class="card">
        <div class="filterbar">
          <div class="fg"><label>企业</label>
            <select id="f-ent"><option value="">全部企业</option>
              ${ents.map(e => `<option value="${e.id}" ${String(e.id) === String(q.qEnt) ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}
            </select></div>
          <div class="fg"><label>状态</label>
            <select id="f-status"><option value="">全部状态</option>
              ${['委托中', '已录入', '审单中', '查验中', '已放行', '已结关', '已撤销'].map((lab, i) => {
                const v = ['entrusted', 'entered', 'reviewing', 'inspecting', 'released', 'closed', 'cancelled'][i];
                return `<option value="${v}" ${v === q.qStatus ? 'selected' : ''}>${lab}</option>`;
              }).join('')}
            </select></div>
        </div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>报关单号</th><th>企业</th><th>方向</th><th>货物 / HS</th><th>口岸</th><th>货值</th><th>税款</th><th>状态</th><th>更新时间</th></tr></thead>
            <tbody>
              ${list.map(d => `
                <tr class="clickable" data-id="${d.id}">
                  <td class="mono">${esc(d.decl_no)}${d.offline_created ? ' <span class="tl-badge-offline">离线补传</span>' : ''}</td>
                  <td>${esc(d.enterprise_name)}</td>
                  <td>${d.ie_type === 'import' ? '📥 进口' : '📤 出口'}</td>
                  <td>${esc(d.cargo_name)}<div class="muted small mono">${esc(d.hs_code)}</div></td>
                  <td>${esc(d.port)}</td>
                  <td class="mono">${fmtMoney(d.total_value)}</td>
                  <td>${taxCell(d)}</td>
                  <td>${statusTag(d.status, d.status_label)}</td>
                  <td class="small muted">${fmtDate(d.updated_at)}</td>
                </tr>`).join('') || '<tr><td colspan="9" class="muted" style="text-align:center;padding:26px">没有符合条件的报关单</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>`;

    view.querySelectorAll('tr[data-id]').forEach(tr => {
      tr.onclick = () => { location.hash = `#/declarations/${tr.dataset.id}`; };
    });
    view.querySelector('#f-ent').onchange = e => {
      location.hash = `#/declarations?${new URLSearchParams(
        Object.assign({}, e.target.value ? { ent: e.target.value } : {},
          q.qStatus ? { status: q.qStatus } : {})).toString()}`;
    };
    view.querySelector('#f-status').onchange = e => {
      location.hash = `#/declarations?${new URLSearchParams(
        Object.assign({}, q.qEnt ? { ent: q.qEnt } : {},
          e.target.value ? { status: e.target.value } : {})).toString()}`;
    };
    if (isBroker) view.querySelector('#new-decl').onclick = () => openDeclModal(ents);
    view.querySelectorAll('[data-del-draft]').forEach(b => {
      b.onclick = () => { Offline.removeDraft(b.dataset.delDraft); router(); };
    });
  }

  function taxCell(d) {
    if (!d.tax_amount) return '<span class="muted small">—</span>';
    if (d.tax_paid) return `<span class="small">${fmtMoney(d.tax_amount)}<br><span class="dot green">已缴</span></span>`;
    const due = d.tax_due_date ? new Date(d.tax_due_date) : null;
    const overdue = due && due < new Date();
    return `<span class="small ${overdue ? 'tax-line overdue' : ''}">${fmtMoney(d.tax_amount)}<br>
      <span class="dot ${overdue ? 'red' : 'gray'}">${overdue ? '🔴已逾期' : '未缴'}</span></span>`;
  }

  /* ---------- 立项 / 离线起草弹窗 ---------- */

  async function openDeclModal(ents) {
    const activeEnts = ents.filter(e => e.status === 'active');
    const contracts = await Api.get('/contracts').catch(() => []);
    const m = modal({
      title: '报关单立项派单（委托链路：备案 → 合同 → 立项）',
      wide: true,
      body: `
        ${!navigator.onLine ? '<div class="warn-box">📵 检测到当前离线：提交将保存为本地离线草稿（带稳定幂等钥匙），联网后自动补传，服务端不会重复建单。</div>' : ''}
        <div class="form-grid">
          <div><label>委托企业（须已备案）</label>
            <select id="d-ent">${activeEnts.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join('')}</select></div>
          <div><label>生效委托合同</label><select id="d-contract"></select></div>
          <div><label>进出口方向</label><select id="d-ie"><option value="import">📥 进口</option><option value="export">📤 出口</option></select></div>
          <div><label>申报口岸</label><input id="d-port" value="${esc(Api.user.port || '盐田港')}"/></div>
          <div class="full"><label>货物名称</label><input id="d-cargo" placeholder="如：贴片电容 0402 系列"/></div>
          <div><label>HS 编码</label><input id="d-hs" placeholder="10 位商品编码"/></div>
          <div><label>数量</label><input id="d-qty" placeholder="如 2000000个"/></div>
          <div><label>货值（CNY）</label><input id="d-val" type="number" value="0"/></div>
          <div><label>币种</label><input id="d-cur" value="CNY"/></div>
          <div class="full"><label>备注</label><input id="d-remark"/></div>
        </div>
        <div id="d-chain-err"></div>`,
      footer: `<button class="btn" id="d-cancel">取消</button>
               <button class="btn gold" id="d-offline">📱 存为离线草稿</button>
               <button class="btn solid" id="d-submit">提交立项并派给自己</button>`,
    });
    const $ent = m.bodyEl.querySelector('#d-ent');
    const $contract = m.bodyEl.querySelector('#d-contract');
    function fillContracts() {
      const mine = contracts.filter(c => String(c.enterprise_id) === String($ent.value));
      const active = mine.filter(c => c.status === 'active');
      $contract.innerHTML = active.length
        ? active.map(c => `<option value="${c.id}">${esc(c.contract_no)}（已生效）</option>`).join('')
        : '<option value="">⚠️ 该企业无生效合同</option>';
    }
    $ent.onchange = fillContracts;
    fillContracts();

    function collect() {
      return {
        enterprise_id: Number($ent.value),
        contract_id: Number($contract.value),
        ie_type: m.bodyEl.querySelector('#d-ie').value,
        port: m.bodyEl.querySelector('#d-port').value.trim(),
        cargo_name: m.bodyEl.querySelector('#d-cargo').value.trim(),
        hs_code: m.bodyEl.querySelector('#d-hs').value.trim(),
        qty: m.bodyEl.querySelector('#d-qty').value.trim(),
        total_value: Number(m.bodyEl.querySelector('#d-val').value || 0),
        currency: m.bodyEl.querySelector('#d-cur').value.trim(),
        remark: m.bodyEl.querySelector('#d-remark').value.trim(),
      };
    }

    m.footEl.querySelector('#d-cancel').onclick = m.close;
    m.footEl.querySelector('#d-offline').onclick = () => {
      const draft = collect();
      if (!draft.cargo_name || !draft.port) { toast('货物名称和口岸为必填', 'warn'); return; }
      const saved = Offline.saveDraft(draft);
      m.close();
      toast(`已存入离线起草箱（${saved.client_ref}），联网后自动补传`, 'warn');
      router();
    };
    m.footEl.querySelector('#d-submit').onclick = async () => {
      const body = collect();
      if (!body.cargo_name || !body.port) { toast('货物名称和口岸为必填', 'warn'); return; }
      if (!body.contract_id) {
        m.bodyEl.querySelector('#d-chain-err').innerHTML =
          '<div class="danger-box">链路拦截：该企业没有「已生效」的委托合同，请先到「客户与委托」完成签署。</div>';
        return;
      }
      if (!navigator.onLine) {
        Offline.saveDraft(body);
        m.close();
        toast('当前离线，已自动转入离线起草箱', 'warn');
        router();
        return;
      }
      try {
        const r = await Api.post('/declarations', body, { idempotencyKey: Api.idemKey('create-decl') });
        m.close();
        if (r.deduplicated || r.idempotency?.replayed) {
          toast(r.idempotency?.notice || r.hint || '幂等：该委托已立项，未重复建单', 'warn');
        } else toast(`立项成功：${r.declaration.decl_no}，已派单给你`, 'success');
        location.hash = `#/declarations/${r.declaration.id}`;
      } catch (e) {
        m.bodyEl.querySelector('#d-chain-err').innerHTML =
          `<div class="danger-box">🚫 ${esc(e.reason)}</div>`;
      }
    };
  }

  /* ================= 报关单详情 ================= */

  // hash 形如 #/declarations/12  → 详情；#/declarations?ent=.. → 列表
  async function detailRoute(rest) {
    const id = rest[0];
    const view = shell('/declarations');
    view.innerHTML = loading();
    let data, staleAt = null;
    try {
      data = await Api.get(`/declarations/${id}`);
      Offline.saveSnapshot('decl-' + id, data);
    } catch (e) {
      if (e.code === 'network_offline') {
        const snap = Offline.getSnapshot('decl-' + id);
        if (snap) { data = snap.data; staleAt = snap.saved_at; toast('离线状态：展示的是缓存快照，状态流转已转入本地队列，恢复后合并', 'warn'); }
        else throw e;
      } else if (e.status === 403 || e.status === 404) {
        // 越权 / 不存在 —— 必须渲染明确错误态，不允许白屏
        view.innerHTML = '';
        view.appendChild(el(`
          <div class="error-state">
            <div class="ico">🚫</div>
            <h3>${e.status === 403 ? '越权访问已拦截' : '报关单不存在'}</h3>
            <div class="reason"><b>错误码：</b>${esc(e.code || 'forbidden')}<br/><b>原因：</b>${esc(e.reason)}</div>
            <div style="display:flex;gap:10px;justify-content:center">
              <button class="btn" id="d-back">返回列表</button>
              <button class="btn solid" id="d-dash">回作战台</button>
            </div>
          </div>`));
        view.querySelector('#d-back').onclick = () => location.hash = '#/declarations';
        view.querySelector('#d-dash').onclick = () => location.hash = '#/dashboard';
        return;
      } else throw e;
    }
    renderDetail(view, data, staleAt);
  }

  routes['/declarations'] = async (rest) => {
    if (rest && rest.length) return detailRoute(rest);
    return listRoute();
  };

  async function listRoute() {
    const view = shell('/declarations');
    const params = new URLSearchParams((location.hash.split('?')[1] || ''));
    const qEnt = params.get('ent') || '';
    const qStatus = params.get('status') || '';
    view.innerHTML = loading();
    try {
      const qs = new URLSearchParams();
      if (qEnt) qs.set('enterprise_id', qEnt);
      if (qStatus) qs.set('status', qStatus);
      const [list, ents] = await Promise.all([
        Api.get('/declarations' + (qs.toString() ? '?' + qs.toString() : '')),
        Api.get('/enterprises').catch(() => []),
      ]);
      listCache.enterprises = ents;
      renderDeclList(view, list, ents, { qEnt, qStatus }, null);
    } catch (e) {
      if (e.code === 'network_offline') {
        const snap = Offline.getSnapshot('decl-list');
        const ents = listCache.enterprises.length ? listCache.enterprises : await Api.get('/enterprises').catch(() => []);
        if (snap) { renderDeclList(view, snap.data, ents, { qEnt, qStatus }, snap.saved_at); toast('离线：展示缓存快照', 'warn'); }
        else throw e;
      } else throw e;
    }
  }

  function renderDetail(view, data, staleAt) {
    const d = data.declaration;
    const user = Api.user;
    const queued = Offline.listActions(d.id);
    const canReveal = user.role === 'broker' || user.role === 'supervisor';

    view.innerHTML = `
      <div class="page-head">
        <div>
          <h2>📄 ${esc(d.decl_no)} ${statusTag(d.status, d.status_label)}
            ${d.offline_created ? '<span class="tl-badge-offline">离线起草补传</span>' : ''}</h2>
          <div class="desc">
            企业：<b>${esc(d.enterprise_name)}</b>
            <span class="mask-hint small muted">（默认脱敏显示）</span>
            ${canReveal ? '<button class="btn sm" id="reveal-name">🔓 二次确认查看全名</button>' : ''}
            · 合同 <span class="mono">${esc(data.contract_no || '—')}</span>
            · 版本 v${d.version}
            ${staleAt ? '· ' + offlineStamp(staleAt) : ''}
          </div>
        </div>
        <button class="btn" id="back-list">← 返回列表</button>
      </div>

      ${staleAt ? `<div class="offline-panel">
        <div class="op-title">📵 离线模式：以下为非实时状态（缓存快照，截至 ${fmtDate(staleAt)}），不代表海关/本行当前最新状态。</div>
        <div class="small">系统不会用这份旧状态冒充实时结果。断网期间执行的流转会进入下方本地队列，恢复网络后按顺序提交、逐条去重合并；冲突时交人工确认，绝不覆盖服务端最新状态。</div>
      </div>` : ''}

      <div class="two-col">
        <div>
          <div class="card">
            <h3>📦 申报要素</h3>
            <dl class="kv">
              <dt>方向/口岸</dt><dd>${d.ie_type === 'import' ? '📥 进口' : '📤 出口'} · ${esc(d.port)}</dd>
              <dt>货物名称</dt><dd>${esc(d.cargo_name)}</dd>
              <dt>HS 编码</dt><dd class="mono">${esc(d.hs_code)}</dd>
              <dt>数量</dt><dd>${esc(d.qty)}</dd>
              <dt>货值</dt><dd class="mono">${fmtMoney(d.total_value)} ${esc(d.currency)}</dd>
              <dt>报关员</dt><dd>${esc(d.broker_name || '待派单')}</dd>
              <dt>备注</dt><dd>${esc(d.remark || '—')}</dd>
            </dl>
          </div>
          <div class="card">
            <h3>💰 征税</h3>
            <dl class="kv">
              <dt>应征税款</dt><dd class="mono">${d.tax_amount ? fmtMoney(d.tax_amount) : '未生成'}</dd>
              <dt>缴款期限</dt><dd>${d.tax_due_date ? fmtDate(d.tax_due_date) : '—'} ${taxDueFlag(d)}</dd>
              <dt>缴税状态</dt><dd>${d.tax_paid ? '<span class="dot green">已缴 ' + fmtDate(d.tax_paid_at) + '</span>' : '<span class="dot gray">未缴</span>'}</dd>
            </dl>
            ${d.tax_amount && !d.tax_paid && ['broker', 'enterprise'].includes(user.role)
              ? '<button class="btn gold" id="pay-tax" style="margin-top:10px">登记缴税</button>' : ''}
          </div>
          <div class="card">
            <h3>🛃 查验排期</h3>
            <div id="detail-inspection"></div>
          </div>
          <div class="card">
            <h3>🔄 状态流转（合规操作）</h3>
            <div id="actions" style="display:flex;gap:8px;flex-wrap:wrap"></div>
            <div id="assign-slot" style="margin-top:10px"></div>
            ${queued.length ? `
              <div class="offline-panel" style="margin-top:12px">
                <div class="op-title">⏳ 本地待合并动作（${queued.length}）</div>
                ${queued.map(q => `<div class="offline-queue-item"><span>📱</span>
                  <span>${esc(q.action)}</span><span class="muted small">${fmtDate(q.client_at)}</span></div>`).join('')}
              </div>` : ''}
            ${!navigator.onLine ? '<div class="small muted" style="margin-top:8px">📵 离线中：操作将先排队，联网后合并。</div>' : ''}
          </div>
        </div>

        <div>
          <div class="card">
            <h3>🕰️ 全生命周期留痕</h3>
            <div class="timeline">
              ${data.events.slice().reverse().map(ev => `
                <div class="tl-item">
                  <div class="tl-title">${esc(ev.from_label)} → ${esc(ev.to_label)}
                    ${ev.is_offline ? '<span class="tl-badge-offline">离线补传</span>' : ''}</div>
                  <div class="tl-meta">${esc(ev.actor_name)} · ${fmtDate(ev.created_at)}${ev.note ? ' · ' + esc(ev.note) : ''}</div>
                </div>`).join('')}
            </div>
          </div>
        </div>
      </div>`;

    view.querySelector('#back-list').onclick = () => location.hash = '#/declarations';
    const actionsEl = view.querySelector('#actions');
    // 离线时仍允许基于快照动作排队（提交时不触网，恢复后合并）；文案会标明离线排队
    const allowed = data.allowed_actions || [];
    if (!allowed.length) {
      actionsEl.innerHTML = `<span class="muted small">当前状态/角色下无可执行流转（或已为终态）。</span>`;
    } else {
      allowed.forEach(a => {
        const danger = a.to_status === 'cancelled';
        const b = el(`<button class="btn ${danger ? 'danger' : 'solid'}">${esc(a.label)}</button>`);
        b.onclick = () => doTransition(d, a, danger);
        actionsEl.appendChild(b);
      });
    }

    // 委托中：本行派单/改派给报关员
    const assignSlot = view.querySelector('#assign-slot');
    if (assignSlot) {
      if (!staleAt && d.status === 'entrusted' && ['broker', 'supervisor'].includes(user.role)) {
        const ab = el(`<button class="btn gold" style="margin-top:10px">🧑‍💼 ${d.broker_name ? '改派报关员' : '指派报关员'}</button>`);
        ab.onclick = () => openAssign(d);
        assignSlot.appendChild(ab);
      }
    }

    const payBtn = view.querySelector('#pay-tax');
    if (payBtn) payBtn.onclick = async () => {
      const note = await confirmNote({ title: '登记缴税', message: `确认该单税款 ${fmtMoney(d.tax_amount)} 已实缴？`, placeholder: '如：税款已通过电子支付平台扣缴，国库待销号' });
      if (note === null) return;
      try {
        await Api.post(`/declarations/${d.id}/tax`, { paid: true, note });
        toast('缴税登记成功', 'success');
        router();
      } catch (e) { toast(e.reason, 'error'); }
    };

    const rv = view.querySelector('#reveal-name');
    if (rv) rv.onclick = () => openReveal(d.enterprise_id, d.enterprise_name, data);

    // 查验排期卡（当前排期 / 该单无排期 / 排期全取消 三种独立状态）
    if (window.SchedulingModule) window.SchedulingModule.bindDetail(view, data);
  }

  function taxDueFlag(d) {
    if (!d.tax_due_date || d.tax_paid) return '';
    const days = (new Date(d.tax_due_date) - new Date()) / 86400;
    if (days < 0) return '<span class="dot red">🔴 已逾期 ' + Math.ceil(-days) + ' 天</span>';
    if (days <= 3) return '<span class="dot amber">🟡 ' + Math.ceil(days) + ' 天后到期</span>';
    return '';
  }

  /* ---------- 流转：在线乐观锁 / 离线排队 ---------- */

  async function openAssign(d) {
    let brokers = [];
    try { brokers = await Api.get('/users/brokers'); } catch (e) { toast(e.reason, 'error'); return; }
    const m = modal({
      title: '🧑‍💼 指派报关员',
      body: `
        <div class="warn-box">报关单 <b>${esc(d.decl_no)}</b> 当前为「委托中」，指派报关员后由其负责录入与后续申报。</div>
        <label>报关员</label>
        <select id="as-broker">${brokers.map(b =>
          `<option value="${b.id}" ${b.id === d.broker_id ? 'selected' : ''}>${esc(b.display_name)}（${esc(b.port || '未分港口')}）</option>`).join('')}</select>`,
      footer: '<button class="btn" id="as-cancel">取消</button><button class="btn solid" id="as-ok">确认派单</button>',
    });
    m.mask.querySelector('#as-cancel').onclick = m.close;
    m.mask.querySelector('#as-ok').onclick = async () => {
      const broker_id = Number(m.mask.querySelector('#as-broker').value);
      try {
        await Api.post(`/declarations/${d.id}/assign`, { broker_id });
        m.close();
        toast('派单成功', 'success');
        router();
      } catch (e) { toast(e.reason, 'error'); }
    };
  }

  async function doTransition(d, action, danger) {
    const note = await confirmNote({
      title: action.label,
      message: `将把报关单 ${d.decl_no} 从「${d.status_label}」流转到下一环节，操作不可非法回退，确认继续？`,
      danger,
      confirmText: danger ? '确认撤销' : '确认执行',
      placeholder: '请填写操作依据/说明',
    });
    if (note === null) return;

    if (!navigator.onLine) {
      Offline.queueAction(d.id, action.action, note);
      toast(`离线已排队：${action.label}（恢复网络后自动提交合并，带事件ID去重）`, 'warn');
      router();
      return;
    }

    try {
      const r = await Api.post(`/declarations/${d.id}/transition`, {
        action: action.action, note, expected_version: d.version,
      });
      toast(`流转成功：${r.declaration.status_label}（v${r.declaration.version}）`, 'success');
      router();
    } catch (e) {
      if (e.code === 'version_conflict') {
        openMergeConflict(d, action, note, e);
      } else {
        // 非法回退 / 终态 / 越权 —— 服务端给的原因原样展示
        modal({
          title: '🚫 操作被合规规则拦下',
          body: `<div class="danger-box">${esc(e.reason)}</div>
                 <div class="small muted">未对单据做任何变更。请依据当前状态选择允许的下一步。</div>`,
          footer: '<button class="btn solid" id="ok">我知道了</button>',
        }).mask.querySelector('#ok').onclick = () => { router(); };
      }
    }
  }

  async function openMergeConflict(d, action, note, err) {
    // 拉最新快照，让用户基于服务端现状决定是否仍要执行
    let latest;
    try { latest = await Api.get(`/declarations/${d.id}`); } catch (e2) { toast(e2.reason, 'error'); return; }
    const ld = latest.declaration;
    const stillAllowed = (latest.allowed_actions || []).some(a => a.action === action.action);
    const m = modal({
      title: '🔀 状态版本冲突 · 需合并确认',
      body: `
        <div class="danger-box">${esc(err.reason)}</div>
        <div class="form-grid">
          <div><label>你本地看到的</label><div class="kv" style="grid-template-columns:1fr"><dd>${esc(d.status_label)} · v${d.version}</dd></div></div>
          <div><label>服务端最新</label><div class="kv" style="grid-template-columns:1fr"><dd>${esc(ld.status_label)} · v${ld.version}</dd></div></div>
        </div>
        ${stillAllowed
          ? `<div class="warn-box">服务端当前状态仍然允许执行「${esc(action.label)}」。确认后将基于最新版本提交，不会覆盖他人已做的变更。</div>`
          : '<div class="danger-box">服务端最新状态下该动作已不适用（可能已被海关推进/退回），不能强行提交，以免非法回退。</div>'}
        <div class="small muted">你的操作说明：${esc(note)}</div>`,
      footer: stillAllowed
        ? '<button class="btn" id="c">取消</button><button class="btn solid" id="merge-ok">基于最新状态合并执行</button>'
        : '<button class="btn solid" id="c">刷新查看最新状态</button>',
    });
    m.mask.querySelector('#c').onclick = () => { m.close(); router(); };
    const ok = m.mask.querySelector('#merge-ok');
    if (ok) ok.onclick = async () => {
      try {
        const r = await Api.post(`/declarations/${d.id}/transition`, {
          action: action.action, note: note + '（版本冲突合并后重提）', expected_version: ld.version,
        });
        m.close();
        toast(`合并提交成功：${r.declaration.status_label}（v${r.declaration.version}）`, 'success');
        router();
      } catch (e2) { m.close(); toast(e2.reason, 'error'); router(); }
    };
  }

  /* ---------- 二次确认看全名 ---------- */

  function openReveal(entId, maskedName) {
    const m = modal({
      title: `🔓 二次确认查看企业全名（当前：${maskedName}）`,
      body: `
        <div class="warn-box">⚠️ 企业名称属脱敏保护信息。查看全名必须填写具体业务理由，系统将<b>留痕</b>（查看人、时间、理由），供监管员审计。</div>
        <label>查看理由（不少于 10 字）</label>
        <textarea id="rv-reason" rows="3" placeholder="如：该单查验异常，需核对合同抬头与报关企业全称是否一致"></textarea>`,
      footer: '<button class="btn" id="rv-cancel">取消</button><button class="btn solid" id="rv-ok">确认查看并留痕</button>',
    });
    m.mask.querySelector('#rv-cancel').onclick = m.close;
    m.mask.querySelector('#rv-ok').onclick = async () => {
      const reason = m.mask.querySelector('#rv-reason').value.trim();
      if (reason.length < 10) { toast('理由不少于 10 个字，二次确认不通过', 'warn'); return; }
      try {
        const r = await Api.post('/enterprises/reveal', { enterprise_id: entId, reason });
        m.close();
        modal({
          title: '企业全名（本次查看已留痕）',
          body: `
            <dl class="kv">
              <dt>企业全称</dt><dd style="font-weight:700">${esc(r.name_full)}</dd>
              <dt>统一信用代码</dt><dd class="mono">${esc(r.credit_code)}</dd>
            </dl>
            <div style="margin-top:12px;background:var(--gray-50);border-radius:8px;padding:10px 12px;font-size:12px">
              🛡️ ${esc(r.audit.notice)}<br/>
              查看人：${esc(r.audit.viewer)} · 时间：${fmtDate(r.audit.time)}<br/>理由：${esc(r.audit.reason)}
            </div>`,
          footer: '<button class="btn solid" id="ok">我知道了</button>',
        }).mask.querySelector('#ok').onclick = () => router();
      } catch (e) { toast(e.reason, 'error'); }
    };
  }

  /* ================= 客户与委托 ================= */

  route('/clients', async () => {
    const view = shell('/clients');
    view.innerHTML = loading();
    const [ents, contracts] = await Promise.all([
      Api.get('/enterprises'), Api.get('/contracts'),
    ]);
    renderClients(view, ents, contracts);
  });

  function renderClients(view, ents, contracts) {
    const user = Api.user;
    const canFile = ['broker', 'supervisor'].includes(user.role);
    const canApprove = ['supervisor', 'customs'].includes(user.role);
    const canContract = ['broker', 'supervisor'].includes(user.role);
    view.innerHTML = `
      <div class="page-head">
        <div><h2>🤝 客户与委托</h2>
          <div class="desc">委托链路：企业备案 → 签委托合同 → 报关单立项派单。企业名默认「缩写+编号」脱敏，全名需二次确认。</div></div>
        <div style="display:flex;gap:8px">
          ${canFile ? '<button class="btn solid" id="file-ent">＋ 企业备案</button>' : ''}
        </div>
      </div>

      <div class="card">
        <h3>🏢 企业客户（脱敏展示）</h3>
        <div class="table-wrap"><table class="data">
          <thead><tr><th>脱敏名</th><th>海关注册编码</th><th>状态</th><th>联系人</th><th>备案时间</th><th style="width:260px">操作</th></tr></thead>
          <tbody>
            ${ents.map(e => `
              <tr>
                <td><b>${esc(e.name)}</b>
                  <span class="mask-hint" title="全称受脱敏保护，报关员需二次确认+填理由留痕后查看">🔒 已脱敏</span>
                  ${['broker', 'supervisor'].includes(user.role) ? '<button class="btn sm" data-reveal="' + e.id + '" data-masked="' + esc(e.name) + '">全名</button>' : ''}</td>
                <td class="mono">${esc(e.code)}</td>
                <td><span class="pill ${e.status}">${esc(e.status_label)}</span>
                  ${e.reject_reason ? `<div class="small" style="color:var(--red-600)">驳回：${esc(e.reject_reason)}</div>` : ''}</td>
                <td>${esc(e.contact_person)}<div class="muted small">${esc(e.contact_phone)}</div></td>
                <td class="small">${fmtDate(e.filed_at)}</td>
                <td>
                  ${e.status === 'filing' && canApprove ? `<button class="btn sm solid" data-approve="${e.id}">审核通过</button>
                    <button class="btn sm danger" data-reject="${e.id}">驳回</button>` : ''}
                  ${e.status === 'active' && canContract ? `<button class="btn sm" data-contract="${e.id}">拟定委托合同</button>` : ''}
                  <button class="btn sm" data-decls="${e.id}">其报关单</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table></div>
      </div>

      <div class="card">
        <h3>📝 委托合同</h3>
        <div class="table-wrap"><table class="data">
          <thead><tr><th>合同号</th><th>企业（脱敏）</th><th>状态</th><th>委托范围</th><th>签署时间</th><th>操作</th></tr></thead>
          <tbody>
            ${contracts.map(c => `
              <tr>
                <td class="mono">${esc(c.contract_no)}</td>
                <td>${esc(c.enterprise_name)}</td>
                <td><span class="pill ${c.status === 'active' ? 'active' : c.status === 'terminated' ? 'rejected' : 'filing'}">${esc(c.status_label)}</span></td>
                <td class="small muted">${esc(c.scope_text)}</td>
                <td class="small">${fmtDate(c.signed_at)}</td>
                <td>${c.status === 'pending' && (['broker', 'supervisor'].includes(user.role)
                    || (user.role === 'enterprise' && c.enterprise_id === (Api.user.enterprise_id || 0)))
                  ? `<button class="btn sm solid" data-sign="${c.id}">签署生效</button>`
                  : '—'}</td>
              </tr>`).join('')}
          </tbody>
        </table></div>
      </div>`;

    view.querySelectorAll('[data-reveal]').forEach(b => {
      b.onclick = () => openReveal(b.dataset.reveal, b.dataset.masked);
    });
    view.querySelectorAll('[data-approve]').forEach(b => b.onclick = async () => {
      try { await Api.post(`/enterprises/${b.dataset.approve}/approve`, {}); toast('备案审核通过，企业可签委托合同', 'success'); router(); }
      catch (e) { toast(e.reason, 'error'); }
    });
    view.querySelectorAll('[data-reject]').forEach(b => b.onclick = async () => {
      const reason = await confirmNote({ title: '驳回备案', message: '确认驳回该企业的海关备案？', placeholder: '请填写驳回原因（如：资料不齐/信用代码核验不通过）', danger: true, confirmText: '确认驳回' });
      if (reason === null) return;
      try { await Api.post(`/enterprises/${b.dataset.reject}/reject?reason=${encodeURIComponent(reason)}`, {}); toast('已驳回', 'warn'); router(); }
      catch (e) { toast(e.reason, 'error'); }
    });
    view.querySelectorAll('[data-contract]').forEach(b => b.onclick = async () => {
      const entId = Number(b.dataset.contract);
      try {
        const r = await Api.post('/contracts', { enterprise_id: entId });
        toast(`合同 ${r.contract.contract_no} 已拟定（待签署）`, 'success');
        router();
      } catch (e) {
        modal({ title: '🚫 委托链路拦截', body: `<div class="danger-box">${esc(e.reason)}</div>`,
          footer: '<button class="btn solid" id="ok">知道了</button>' }).mask.querySelector('#ok').onclick = () => {};
      }
    });
    view.querySelectorAll('[data-sign]').forEach(b => b.onclick = async () => {
      try {
        const r = await Api.post(`/contracts/${b.dataset.sign}/sign`, {});
        toast(r.hint || '合同已生效', 'success');
        router();
      } catch (e) { toast(e.reason, 'error'); }
    });
    view.querySelectorAll('[data-decls]').forEach(b => {
      b.onclick = () => { location.hash = `#/declarations?ent=${b.dataset.decls}`; };
    });
    const fileBtn = document.getElementById('file-ent');
    if (fileBtn) fileBtn.onclick = openFileEnterprise;
  }

  function openFileEnterprise() {
    const m = modal({
      title: '企业备案（提交后为「备案中」，审核通过才可签委托）',
      body: `
        <div class="form-grid">
          <div><label>企业全称</label><input id="f-name" placeholder="如：XX市XX国际贸易有限公司"/></div>
          <div><label>海关注册编码</label><input id="f-code" placeholder="10 位编码"/></div>
          <div class="full"><label>统一社会信用代码</label><input id="f-credit" placeholder="18 位"/></div>
          <div><label>联系人</label><input id="f-person"/></div>
          <div><label>联系电话</label><input id="f-phone"/></div>
        </div>
        <div id="f-err"></div>`,
      footer: '<button class="btn" id="f-cancel">取消</button><button class="btn solid" id="f-ok">提交备案</button>',
    });
    m.mask.querySelector('#f-cancel').onclick = m.close;
    m.mask.querySelector('#f-ok').onclick = async () => {
      const body = {
        name_full: m.bodyEl.querySelector('#f-name').value.trim(),
        code: m.bodyEl.querySelector('#f-code').value.trim(),
        credit_code: m.bodyEl.querySelector('#f-credit').value.trim(),
        contact_person: m.bodyEl.querySelector('#f-person').value.trim(),
        contact_phone: m.bodyEl.querySelector('#f-phone').value.trim(),
      };
      if (body.name_full.length < 4 || body.code.length < 6 || body.credit_code.length < 6) {
        toast('全称/编码请按要求填写完整', 'warn'); return;
      }
      try {
        const r = await Api.post('/enterprises/file', body);
        m.close();
        toast(r.hint || '已提交备案', 'success');
        router();
      } catch (e) {
        m.bodyEl.querySelector('#f-err').innerHTML = `<div class="danger-box">${esc(e.reason)}</div>`;
      }
    };
  }

  /* ================= 留痕审计 ================= */

  route('/audit', async () => {
    const view = shell('/audit');
    view.innerHTML = loading();
    try {
      const rows = await Api.get('/enterprises/reveals/audit');
      view.innerHTML = `
        <div class="page-head"><div><h2>🛡️ 企业全名查看留痕</h2>
          <div class="desc">报关员每次查看脱敏企业全称均强制二次确认、填写理由并留痕，可审计追溯。</div></div></div>
        <div class="card"><div class="table-wrap"><table class="data">
          <thead><tr><th>时间</th><th>报关员</th><th>企业全称</th><th>查看理由</th></tr></thead>
          <tbody>
            ${rows.map(r => `<tr><td class="small">${fmtDate(r.created_at)}</td><td>${esc(r.broker_name)}</td>
              <td>${esc(r.enterprise_name)}</td><td>${esc(r.reason)}</td></tr>`).join('')
              || '<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">尚无查看记录（演示：用报关员账号打开任一报关单 → 二次确认查看全名）</td></tr>'}
          </tbody>
        </table></div></div>`;
    } catch (e) {
      view.innerHTML = '';
      view.appendChild(el(`<div class="error-state"><div class="ico">🚫</div><h3>无权访问</h3>
        <div class="reason">${esc(e.reason)}</div></div>`));
    }
  });

  /* ---------------- 离线恢复自动合并 ---------------- */

  let flushing = false;
  async function triggerFlush() {
    if (flushing || !navigator.onLine) return;
    if (Offline.pendingCount() === 0) { renderBanner(true); return; }
    flushing = true;
    renderBanner(true);
    const r = await Offline.flushAll();
    flushing = false;
    renderBanner(navigator.onLine);

    const draftOk = r.drafts.filter(x => x.ok).length;
    const actApplied = r.actions.filter(x => x.ok).reduce((s, x) => s + (x.merged.applied?.length || 0), 0);
    const conflicts = r.actions.flatMap(x => x.ok ? (x.merged.conflicts || []) : [{ reason: x.error?.reason || '同步失败' }]);
    const dupSkips = r.actions.flatMap(x => x.ok ? (x.merged.skipped_duplicates || []) : []);

    if (draftOk) toast(`✅ ${draftOk} 张离线草稿已补传立项（幂等，未产生重复单）`, 'success');
    if (actApplied) toast(`✅ ${actApplied} 条离线流转已合并入状态机`, 'success');
    if (dupSkips.length) toast(`🟡 ${dupSkips.length} 条重复动作已幂等跳过`, 'warn');
    if (conflicts.length) {
      modal({
        title: '⚠️ 离线合并存在冲突，需人工确认',
        wide: true,
        body: `<div class="sync-report">
          <p class="small">以下离线动作与服务端最新状态冲突（可能服务端已被推进，本地动作构成回退），<b>系统未强行覆盖</b>：</p>
          <ul>${conflicts.map(c => `<li class="conf-item">${esc(c.reason)}</li>`).join('')}</ul></div>`,
        footer: '<button class="btn solid" id="ok">查看最新状态</button>',
      }).mask.querySelector('#ok').onclick = () => router();
    }
    router();
  }

  /* ---------------- 启动 ---------------- */

  // 向查验排期模块注入应用上下文（路由注册、外壳、复用 UI 件）
  if (window.SchedulingModule) {
    window.SchedulingModule.register({
      route, shell, Api,
      el, esc, modal, toast, fmtDate, fmtMoney, confirmNote, loading,
      rerun: () => window.dispatchEvent(new Event('hashchange')),
    });
  }

  Offline.init();
  Offline.onChange((online) => {
    renderBanner(online);
    if (online) {
      toast('网络已恢复，开始合并离线变更…', 'success');
      triggerFlush();
    } else {
      toast('已进入离线模式：可起草/排队，状态以缓存水印为准，不糊弄', 'warn');
      router();
    }
  });
  renderBanner(navigator.onLine);

  window.addEventListener('hashchange', router);
  if (!Api.loggedIn) location.hash = '#/login';
  router();
})();
