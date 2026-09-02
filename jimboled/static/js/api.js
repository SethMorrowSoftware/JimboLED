/* Small fetch wrapper. Every mutating call carries the anti-CSRF header. */
(function () {
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }
  async function request(method, path, body, opts) {
    opts = opts || {};
    const headers = { 'Accept': 'application/json', 'X-Requested-With': 'JimboLED' };
    let payload;
    if (body instanceof FormData) { payload = body; }
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    let resp;
    try {
      resp = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal: opts.signal, keepalive: !!opts.keepalive });
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new ApiError('Cannot reach JimboLED. Is the Pi switched on and on the same network?', 0);
    }
    let data = null;
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) { try { data = await resp.json(); } catch (e) { data = null; } }
    if (resp.status === 401 && data && data.login) {
      window.location.href = '/login?next=' + encodeURIComponent(location.pathname + location.hash);
      throw new ApiError('Please sign in', 401, data);
    }
    if (!resp.ok) {
      const msg = (data && data.error) || ('Request failed (' + resp.status + ')');
      throw new ApiError(msg, resp.status, data);
    }
    return data;
  }
  window.api = {
    ApiError,
    get: (p, o) => request('GET', p, undefined, o),
    post: (p, b, o) => request('POST', p, b === undefined ? {} : b, o),
    put: (p, b, o) => request('PUT', p, b, o),
    del: (p, o) => request('DELETE', p, undefined, o),
    upload: (p, form) => request('POST', p, form),
  };
})();
