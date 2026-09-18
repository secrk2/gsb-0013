/* 报关通离线管理（口岸无网场景）
 *
 * 三条硬规则：
 * 1. 断网时绝不拿旧状态冒充最新状态 —— 所有缓存快照打「离线数据·截至时间」水印，
 *    且状态区显示离线横幅，禁止假装流转成功。
 * 2. 断网期间的操作只做两件事：①本地起草报关单 ②本地排队流转动作；
 *    每条动作有稳定 client_event_id，恢复后补传，服务端逐条去重。
 * 3. 恢复联网后顺序合并 + 幂等：已应用的跳过、冲突的进冲突清单，绝不产生重复报关单。
 */
(function () {
  'use strict';

  const DRAFTS_KEY = 'bgt_offline_drafts';
  const QUEUE_KEY = 'bgt_offline_queue';
  const SNAP_KEY = 'bgt_snapshots';

  const Offline = {
    listeners: [],
    _online: navigator.onLine,

    get isOnline() { return navigator.onLine; },

    onChange(fn) { this.listeners.push(fn); },
    _emit() { this.listeners.forEach(fn => { try { fn(navigator.onLine); } catch (e) {} }); },

    init() {
      // 仅发出状态变化事件；是否触发 flushAll 由 app 层统一调度，避免重复合并
      window.addEventListener('online', () => { this._online = true; this._emit(); });
      window.addEventListener('offline', () => { this._online = false; this._emit(); });
    },

    /* ---------- 本地存储 ---------- */
    _read(k, d) {
      try { return JSON.parse(localStorage.getItem(k)) || d; } catch { return d; }
    },
    _write(k, v) { localStorage.setItem(k, JSON.stringify(v)); },

    /* ---------- 快照水印（防止拿旧状态糊弄）---------- */
    saveSnapshot(key, data) {
      const all = this._read(SNAP_KEY, {});
      all[key] = { data, saved_at: new Date().toISOString() };
      this._write(SNAP_KEY, all);
    },
    getSnapshot(key) {
      const all = this._read(SNAP_KEY, {});
      return all[key] || null;
    },

    /* ---------- 离线起草报关单 ---------- */
    saveDraft(draft) {
      const drafts = this._read(DRAFTS_KEY, []);
      const item = {
        ...draft,
        client_ref: draft.client_ref || ('DRAFT-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7)),
        idem_key: draft.idem_key || Api.idemKey('create-decl'),
        queued_at: new Date().toISOString(),
      };
      drafts.push(item);
      this._write(DRAFTS_KEY, drafts);
      return item;
    },
    listDrafts() { return this._read(DRAFTS_KEY, []); },
    removeDraft(ref) {
      this._write(DRAFTS_KEY, this._read(DRAFTS_KEY, []).filter(d => d.client_ref !== ref));
    },

    /* ---------- 离线流转动作 ---------- */
    queueAction(declId, action, note) {
      const queue = this._read(QUEUE_KEY, []);
      const item = {
        decl_id: declId,
        action,
        note: note || '',
        client_event_id: 'evt-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8),
        client_at: new Date().toISOString(),
      };
      queue.push(item);
      this._write(QUEUE_KEY, queue);
      return item;
    },
    listActions(declId) {
      return this._read(QUEUE_KEY, []).filter(q => String(q.decl_id) === String(declId));
    },
    listAllActions() { return this._read(QUEUE_KEY, []); },
    _replaceQueue(q) { this._write(QUEUE_KEY, q); },

    pendingCount() {
      return this._read(DRAFTS_KEY, []).length + this._read(QUEUE_KEY, []).length;
    },

    /* ---------- 恢复联网后合并 ---------- */
    async flushAll() {
      if (!navigator.onLine) {
        return { ok: false, reason: 'still-offline', drafts: [], actions: [] };
      }
      const draftResults = [];
      const actionResults = [];

      // 1) 先补传起草的报关单（同一 idem_key / client_ref，服务端幂等）
      const drafts = this.listDrafts();
      for (const d of drafts) {
        try {
          const resp = await Api.post('/declarations', { ...d, offline_drafted: true },
            { idempotencyKey: d.idem_key });
          draftResults.push({ ref: d.client_ref, ok: true, resp });
          this.removeDraft(d.client_ref);
        } catch (e) {
          draftResults.push({ ref: d.client_ref, ok: false, error: e });
          // 业务错误（如合同未生效）不应自动重试到天荒地老；网络错误则保留队列下次再试
          if (e.status > 0 && e.status !== 409 && e.code !== 'network_offline') this.removeDraft(d.client_ref);
        }
      }

      // 2) 按报关单分组补传离线流转动作，服务端状态机逐条重放并去重
      const queue = this.listAllActions();
      const byDecl = {};
      queue.forEach(q => { (byDecl[q.decl_id] = byDecl[q.decl_id] || []).push(q); });

      for (const declId of Object.keys(byDecl)) {
        const events = byDecl[declId].map(q => ({
          client_event_id: q.client_event_id, action: q.action, note: q.note, client_at: q.client_at,
        }));
        try {
          const resp = await Api.post(`/declarations/${declId}/sync-offline`, {
            action: 'sync', offline_events: events,
          });
          const merged = resp.merged || { applied: [], skipped_duplicates: [], conflicts: [] };
          actionResults.push({ decl_id: declId, ok: true, merged });
          // 已应用 / 已跳过（此前同步过）的动作出队；冲突的也出队并在 UI 报告，避免反复重放
          const handled = new Set([
            ...merged.applied.map(a => a.client_event_id),
            ...merged.skipped_duplicates.map(a => a.client_event_id),
            ...merged.conflicts.map(a => a.client_event_id),
          ]);
          const left = this.listAllActions().filter(
            q => !(String(q.decl_id) === String(declId) && handled.has(q.client_event_id)));
          this._replaceQueue(left);
        } catch (e) {
          actionResults.push({ decl_id: declId, ok: false, error: e });
          if (e.status > 0 && e.code !== 'network_offline' && e.status !== 409) {
            // 单据不存在等硬错误：清掉这组，避免死循环；冲突类保留
            const left = this.listAllActions().filter(q => String(q.decl_id) !== String(declId));
            this._replaceQueue(left);
          }
        }
      }

      return { ok: true, drafts: draftResults, actions: actionResults };
    },
  };

  window.Offline = Offline;
})();
