/* UI primitives: DOM helpers, modals, toasts, menus, sliders, tabs. */
(function () {
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function el(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (v === true) node.setAttribute(k, '');
      else node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const throttle = (fn, ms) => { let last = 0, timer, args; return (...a) => { args = a; const now = Date.now(); if (now - last >= ms) { last = now; fn(...args); } else { clearTimeout(timer); timer = setTimeout(() => { last = Date.now(); fn(...args); }, ms - (now - last)); } }; };
  function fmtDuration(s) {
    s = Math.max(0, Math.round(s || 0));
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    if (d) return `${d}d ${hh}h`;
    if (hh) return `${hh}h ${mm}m`;
    if (mm) return `${mm}m ${ss}s`;
    return `${ss}s`;
  }
  function fmtAgo(ts) {
    if (!ts) return 'never';
    const s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 5) return 'just now';
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function fmtBytes(b) { if (b == null) return '–'; const u = ['B', 'KB', 'MB', 'GB']; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; }

  // ---- toasts
  function toast(message, type, ms) {
    type = type || 'info';
    const root = document.getElementById('toast-root');
    const node = el(`<div class="toast ${type}">${icon(type === 'success' ? 'check' : type === 'error' ? 'alert' : 'info')}<div class="grow"></div><button class="btn ghost icon sm" aria-label="Dismiss">${icon('x')}</button></div>`);
    node.querySelector('.grow').textContent = message;
    node.querySelector('button').onclick = () => node.remove();
    root.append(node);
    setTimeout(() => node.remove(), ms || (type === 'error' ? 7000 : 3500));
    return node;
  }
  const notifyError = (err) => { console.error(err); toast(err && err.message ? err.message : String(err), 'error'); };

  // ---- modals
  const stack = [];
  function modal(opts) {
    opts = opts || {};
    const root = document.getElementById('modal-root');
    const backdrop = el(`<div class="modal-backdrop"></div>`);
    const box = h('div', { class: 'modal' + (opts.wide ? ' wide' : '') + (opts.full ? ' full' : ''), role: 'dialog', 'aria-modal': 'true' });
    const head = h('div', { class: 'modal-head' });
    if (opts.icon) head.append(el(`<span class="tile-icon">${icon(opts.icon)}</span>`));
    const title = h('h2', { text: opts.title || '' });
    head.append(title);
    if (opts.headActions) head.append(opts.headActions);
    const closeBtn = el(`<button class="btn ghost icon" aria-label="Close">${icon('x')}</button>`);
    head.append(closeBtn);
    const body = h('div', { class: 'modal-body' });
    if (typeof opts.body === 'string') body.innerHTML = opts.body;
    else if (opts.body instanceof Node) body.append(opts.body);
    box.append(head, body);
    let foot = null;
    if (opts.footer) { foot = h('div', { class: 'modal-foot' }); if (opts.footer instanceof Node) foot.append(opts.footer); else foot.append(...opts.footer); box.append(foot); }
    backdrop.append(box);
    root.append(backdrop);
    const api = {
      el: box, body, head, foot, title, sticky: !!opts.sticky,
      setTitle: (t) => { title.textContent = t; },
      close: () => { if (!backdrop.isConnected) return; backdrop.remove(); const i = stack.indexOf(api); if (i >= 0) stack.splice(i, 1); if (opts.onClose) opts.onClose(); },
    };
    closeBtn.onclick = api.close;
    backdrop.addEventListener('pointerdown', (e) => { if (e.target === backdrop && !opts.sticky) api.close(); });
    stack.push(api);
    if (typeof opts.body === 'function') opts.body(body, api);
    setTimeout(() => { const f = box.querySelector('input:not([type=hidden]), select, textarea, button.primary'); if (f && opts.autofocus !== false && window.matchMedia('(min-width: 640px)').matches) f.focus(); }, 30);
    return api;
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && stack.length) { const top = stack[stack.length - 1]; if (!top.sticky) top.close(); } });

  function confirm(opts) {
    opts = typeof opts === 'string' ? { message: opts } : opts;
    return new Promise((resolve) => {
      const ok = el(`<button class="btn ${opts.danger ? 'danger' : 'primary'}">${esc(opts.okText || 'OK')}</button>`);
      const cancel = el(`<button class="btn">${esc(opts.cancelText || 'Cancel')}</button>`);
      const m = modal({ title: opts.title || 'Are you sure?', body: `<p>${esc(opts.message || '')}</p>`, footer: [cancel, ok], onClose: () => resolve(false) });
      ok.onclick = () => { resolve(true); m.close(); };
      cancel.onclick = () => m.close();
      setTimeout(() => ok.focus(), 40);
    });
  }
  function prompt(opts) {
    return new Promise((resolve) => {
      const input = el(`<input class="input" type="${opts.type || 'text'}" value="${esc(opts.value || '')}" placeholder="${esc(opts.placeholder || '')}" maxlength="${opts.maxlength || 60}">`);
      const ok = el(`<button class="btn primary">${esc(opts.okText || 'Save')}</button>`);
      const cancel = el(`<button class="btn">Cancel</button>`);
      const body = h('label', { class: 'field' }, h('span', { text: opts.label || '' }), input);
      if (opts.hint) body.append(h('span', { class: 'hint', text: opts.hint }));
      let result = null;
      const m = modal({ title: opts.title || '', body, footer: [cancel, ok], onClose: () => resolve(result) });
      ok.onclick = () => { result = input.value; m.close(); };
      cancel.onclick = () => m.close();
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
    });
  }

  // ---- dropdown menu
  function menu(anchor, items) {
    closeMenu();
    const node = h('div', { class: 'menu' });
    for (const it of items) {
      if (it === '-') { node.append(h('hr')); continue; }
      const b = el(`<button class="${it.danger ? 'danger' : ''}">${it.icon ? icon(it.icon) : ''}<span>${esc(it.label)}</span></button>`);
      b.onclick = () => { closeMenu(); it.onClick && it.onClick(); };
      node.append(b);
    }
    document.body.append(node);
    const r = anchor.getBoundingClientRect();
    const w = node.offsetWidth, hgt = node.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - w - 8), top = r.bottom + 6;
    if (top + hgt > window.innerHeight - 8) top = Math.max(8, r.top - hgt - 6);
    node.style.left = left + 'px'; node.style.top = top + 'px';
    setTimeout(() => document.addEventListener('pointerdown', onDocDown, { once: true }), 0);
    function onDocDown(e) { if (!node.contains(e.target)) closeMenu(); else document.addEventListener('pointerdown', onDocDown, { once: true }); }
    window._menu = node;
  }
  function closeMenu() { if (window._menu) { window._menu.remove(); window._menu = null; } }

  // ---- controls
  function setPct(input) { const min = +input.min || 0, max = +input.max || 100; input.style.setProperty('--pct', ((input.value - min) / (max - min) * 100) + '%'); }
  function slider(opts) {
    const wrap = h('div', { class: 'stack', style: { gap: '2px' } });
    const label = h('div', { class: 'slider-label' });
    const val = h('b', { text: opts.format ? opts.format(opts.value) : opts.value });
    if (opts.label != null) { label.append(h('span', { text: opts.label }), val); wrap.append(label); }
    const row = h('div', { class: 'slider-row' });
    if (opts.icon) row.append(el(icon(opts.icon)));
    const input = h('input', { type: 'range', class: 'slider', min: opts.min ?? 0, max: opts.max ?? 255, step: opts.step ?? 1, value: opts.value ?? 0 });
    if (opts.fill) input.style.setProperty('--slider-fill', opts.fill);
    setPct(input);
    let lastSent = null;
    input.addEventListener('input', () => { setPct(input); val.textContent = opts.format ? opts.format(+input.value) : input.value; if (opts.onInput) opts.onInput(+input.value); });
    input.addEventListener('change', () => { if (opts.onChange && lastSent !== input.value) { lastSent = input.value; opts.onChange(+input.value); } });
    row.append(input);
    if (opts.label == null) row.append(h('span', { class: 'val', text: opts.format ? opts.format(opts.value) : opts.value }));
    wrap.append(row);
    wrap.input = input;
    wrap.setValue = (v) => { if (document.activeElement === input) return; input.value = v; setPct(input); const t = opts.format ? opts.format(+v) : v; val.textContent = t; const vv = row.querySelector('.val'); if (vv) vv.textContent = t; };
    return wrap;
  }
  function toggle(on, onChange, cls) {
    const b = h('button', { class: 'toggle ' + (cls || '') + (on ? ' on' : ''), role: 'switch', 'aria-checked': on ? 'true' : 'false' });
    b.onclick = (e) => { e.stopPropagation(); const next = !b.classList.contains('on'); b.classList.toggle('on', next); b.setAttribute('aria-checked', next); onChange && onChange(next); };
    b.setOn = (v) => { b.classList.toggle('on', !!v); b.setAttribute('aria-checked', !!v); };
    return b;
  }
  function tabs(list, active, onSelect) {
    const bar = h('div', { class: 'tabs', role: 'tablist' });
    for (const t of list) {
      const b = h('button', { class: 'tab' + (t.id === active ? ' active' : ''), role: 'tab', text: t.label });
      b.onclick = () => { bar.querySelectorAll('.tab').forEach((x) => x.classList.remove('active')); b.classList.add('active'); onSelect(t.id); };
      bar.append(b);
    }
    return bar;
  }
  function iconPicker(current, onPick) {
    const grid = h('div', { class: 'chips' });
    for (const name of window.ICON_CHOICES) {
      const b = el(`<button class="btn icon ${name === current ? 'active' : ''}" title="${name}">${icon(name)}</button>`);
      b.onclick = () => { grid.querySelectorAll('.btn').forEach((x) => x.classList.remove('active')); b.classList.add('active'); onPick(name); };
      grid.append(b);
    }
    return grid;
  }
  const PALETTE = ['', '#7c5cff', '#22d3ee', '#34d399', '#fbbf24', '#f97316', '#f87171', '#ec4899', '#a78bfa', '#60a5fa', '#e2e8f0'];
  function colorPicker(current, onPick) {
    const grid = h('div', { class: 'chips' });
    for (const c of PALETTE) {
      const b = h('button', { class: 'swatch' + (c === (current || '') ? ' active' : ''), title: c || 'Default accent', style: { background: c || 'linear-gradient(135deg, var(--accent), #22d3ee)' } });
      b.onclick = () => { grid.querySelectorAll('.swatch').forEach((x) => x.classList.remove('active')); b.classList.add('active'); onPick(c); };
      grid.append(b);
    }
    return grid;
  }
  function field(label, control, hint) {
    const f = h('label', { class: 'field' }, h('span', { text: label }), control);
    if (hint instanceof Node) { hint.classList.add('hint'); f.append(hint); }
    else if (hint) f.append(h('span', { class: 'hint', text: hint }));
    return f;
  }
  function busy(btn, promise) {
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = icon('refresh', 'spin') + ' <span>' + esc(btn.dataset.busy || 'Working…') + '</span>';
    return promise.finally(() => { btn.disabled = false; btn.innerHTML = old; });
  }

  window.UI = { esc, el, h, debounce, throttle, fmtDuration, fmtAgo, fmtBytes, toast, notifyError, modal, confirm, prompt, menu, closeMenu, slider, setPct, toggle, tabs, iconPicker, colorPicker, field, busy };
})();
