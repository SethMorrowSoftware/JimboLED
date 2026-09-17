/* Small fetch wrapper. Every mutating call carries the anti-CSRF header. */
(function () {
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }
  // fetch() has no timeout of its own: on flaky Wi-Fi a request can hang for
  // as long as the OS lets it, and one hung request is enough to stop the
  // dashboard refreshing at all. Everything gets a deadline; pass
  // opts.timeout (ms, 0 = none) where a call is legitimately slow.
  const DEFAULT_TIMEOUT_MS = 30000;
  async function request(method, path, body, opts) {
    opts = opts || {};
    const headers = { 'Accept': 'application/json', 'X-Requested-With': 'JimboLED' };
    let payload;
    if (body instanceof FormData) { payload = body; }
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    const ms = opts.timeout === undefined ? DEFAULT_TIMEOUT_MS : opts.timeout;
    const limit = ms > 0 ? new AbortController() : null;
    let timedOut = false;
    const timer = limit ? setTimeout(() => { timedOut = true; limit.abort(); }, ms) : null;
    // Honour a caller's own abort signal as well as our deadline.
    if (limit && opts.signal) {
      if (opts.signal.aborted) limit.abort();
      else opts.signal.addEventListener('abort', () => limit.abort(), { once: true });
    }
    let resp;
    try {
      resp = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal: limit ? limit.signal : opts.signal, keepalive: !!opts.keepalive });
    } catch (err) {
      if (timedOut) throw new ApiError('JimboLED took too long to answer. Trying again…', 0);
      if (err.name === 'AbortError') throw err;
      throw new ApiError('Cannot reach JimboLED. Is the Pi switched on and on the same network?', 0);
    } finally {
      if (timer) clearTimeout(timer);
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
    upload: (p, form, o) => request('POST', p, form, o),
  };
})();
