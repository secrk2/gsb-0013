/* 报关通 API 封装：token 管理、错误归一化、Idempotency-Key。 */
(function () {
  'use strict';

  const TOKEN_KEY = 'bgt_token';
  const USER_KEY = 'bgt_user';

  const Api = {
    get token() { return localStorage.getItem(TOKEN_KEY); },
    setToken(t) { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); },
    get user() {
      try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
    },
    setUser(u) { u ? localStorage.setItem(USER_KEY, JSON.stringify(u)) : localStorage.removeItem(USER_KEY); },
    get loggedIn() { return !!this.token; },
    logoutLocal() { this.setToken(null); this.setUser(null); },

    /** 生成稳定幂等钥匙：同一离线草稿重试始终用同一把 → 服务端去重，绝不重复建单 */
    idemKey(prefix) {
      return `${prefix || 'idem'}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    },

    async request(method, path, body, opts = {}) {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers.Authorization = 'Bearer ' + this.token;
      if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey;

      let res;
      try {
        res = await fetch('/api' + path, {
          method,
          headers,
          body: body ? JSON.stringify(body) : undefined,
        });
      } catch (networkErr) {
        // fetch 直接抛错通常意味着断网 —— 明确告知，而不是拿旧数据冒充成功
        throw {
          code: 'network_offline',
          status: 0,
          reason: '网络不可达（口岸现场可能无信号）。操作未送达服务器，已进入离线队列或请检查网络后重试。',
          raw: networkErr,
        };
      }

      let data = null;
      try { data = await res.json(); } catch { /* 非 JSON */ }

      if (!res.ok) {
        const err = (data && data.error) || {};
        throw {
          code: err.code || 'http_' + res.status,
          status: res.status,
          reason: err.reason || `请求失败（HTTP ${res.status}）`,
          data,
        };
      }
      return data;
    },

    get(path, opts) { return this.request('GET', path, null, opts); },
    post(path, body, opts) { return this.request('POST', path, body, opts); },
  };

  window.Api = Api;
})();
