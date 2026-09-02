/* Application shell: state polling, hash router, dashboard rendering, edit mode. */
(function () {
  const { h, el, esc } = UI;
  const POLL_MS = 2000;
  const App = {
    state: { devices: [], gpio: { switches: [] }, dashboard: null, rev: -1, cfgRev: -1, presets: {} },
    editMode: false,
    tiles: new Map(), // tile id -> {el, kind, update}
    route: { view: 'dashboard' },
  };
  window.App = App;

  // ---------------------------------------------------------- routing
  function parseHash() {
    const hash = location.hash.replace(/^#\/?/, '');
    const [path, query] = hash.split('?');
    const parts = path.split('/').filter(Boolean);
    const q = Object.fromEntries(new URLSearchParams(query || ''));
    if (parts[0] === 'settings') return { view: 'settings', section: parts[1] || 'devices', q };
    if (parts[0] === 'device' && parts[1]) return { view: 'dashboard', device: parts[1], tab: parts[2], q };
    return { view: 'dashboard', q };
  }
  window.addEventListener('hashchange', () => { App.route = parseHash(); render(); });

  // ------------------------------------------------------------ data
  let polling = null, inflight = false, failures = 0;
  async function refresh(full) {
    if (inflight) return; inflight = true;
    try {
      const s = await api.get('/api/state');
      failures = 0;
      const prevCfg = App.state.cfgRev;
      App.state.devices = s.devices; App.state.gpio = s.gpio; App.state.rev = s.rev; App.state.cfgRev = s.cfg_rev; App.state.discovery_running = s.discovery_running;
      // The layout only changes when the configuration changes, so fetch it lazily.
      if (full || !App.state.dashboard || s.cfg_rev !== prevCfg) {
        const d = await api.get('/api/dashboard');
        App.state.dashboard = d.dashboard; App.state.setupComplete = d.setup_complete;
        applyTheme(d.dashboard);
      }
      if (App.route.view === 'dashboard') renderDashboard();
      renderTopbar();
      setOffline(false);
    } catch (err) {
      failures++;
      if (err.status === 401) return;
      if (failures >= 2) setOffline(true);
    } finally { inflight = false; }
  }
  App.refresh = refresh;
  App.patchDevice = (dev) => { const i = App.state.devices.findIndex((d) => d.id === dev.id); if (i >= 0) App.state.devices[i] = { ...App.state.devices[i], ...dev }; else App.state.devices.push(dev); updateTiles(); };
  function setOffline(off) { document.getElementById('topbar').classList.toggle('offline', off); const st = document.getElementById('topbar-status'); if (off) st.innerHTML = `<span class="pill"><span class="dot bad"></span>Reconnecting to JimboLED…</span>`; }
  function applyTheme(dash) {
    document.documentElement.dataset.theme = dash.theme || 'midnight';
    document.documentElement.dataset.density = dash.density || 'comfortable';
    document.documentElement.style.setProperty('--accent', dash.accent || '#7c5cff');
    document.getElementById('brand-title').textContent = dash.title || 'JimboLED';
    document.getElementById('brand-sub').textContent = dash.subtitle || '';
    document.title = dash.title || 'JimboLED';
  }

  // ---------------------------------------------------------- topbar
  function renderTopbar() {
    const st = document.getElementById('topbar-status');
    const devs = App.state.devices, online = devs.filter((d) => d.online).length;
    const onSwitches = (App.state.gpio.switches || []).filter((s) => s.on);
    const pieces = [];
    if (devs.length) pieces.push(`<span class="pill clickable" id="pill-devices" title="Controllers"><span class="dot ${online === devs.length ? 'ok' : online ? 'warn' : 'bad'}"></span>${online}/${devs.length} online</span>`);
    if (onSwitches.length) pieces.push(`<span class="pill" title="Relays energised"><span class="dot ok"></span>${esc(onSwitches.map((s) => s.name).join(', '))} on</span>`);
    if (App.state.gpio.simulated && (App.state.gpio.switches || []).length) pieces.push(`<span class="pill" title="No GPIO hardware – switches are simulated"><span class="dot warn"></span>GPIO simulated</span>`);
    if (App.state.dashboard && App.state.dashboard.show_clock !== false) pieces.push(`<span class="pill nowrap" id="pill-clock">${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>`);
    st.innerHTML = pieces.join('');
    const pd = document.getElementById('pill-devices'); if (pd) pd.onclick = () => { location.hash = '#/settings/devices'; };
    const actions = document.getElementById('topbar-actions');
    if (actions.dataset.rendered !== App.route.view + App.editMode) {
      actions.dataset.rendered = App.route.view + App.editMode;
      actions.innerHTML = '';
      if (App.route.view === 'dashboard') {
        const panic = el(`<button class="btn danger sm" title="Turn every relay and every light off">${icon('power')}<span class="hide-sm">All off</span></button>`);
        panic.onclick = async () => { try { await api.post('/api/system/shutdown-all'); UI.toast('Everything switched off', 'success'); refresh(); } catch (e) { UI.notifyError(e); } };
        const edit = el(`<button class="btn sm ${App.editMode ? 'primary' : ''}" title="Rearrange, resize and hide tiles">${icon(App.editMode ? 'check' : 'edit')}<span class="hide-sm">${App.editMode ? 'Done' : 'Edit layout'}</span></button>`);
        edit.onclick = () => setEditMode(!App.editMode);
        const settings = el(`<a class="btn icon sm ghost" href="#/settings" title="Settings">${icon('settings')}</a>`);
        actions.append(panic, edit, settings);
      } else {
        actions.append(el(`<a class="btn sm" href="#/">${icon('grid')}<span class="hide-sm">Dashboard</span></a>`));
      }
    }
  }
  setInterval(() => { const c = document.getElementById('pill-clock'); if (c) c.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); const t = document.querySelector('.tile.clock .time'); if (t) { t.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); t.nextElementSibling.textContent = new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' }); } }, 1000);

  // ------------------------------------------------------- dashboard
  const main = document.getElementById('main');
  let grid = null;
  function render() {
    renderTopbar();
    if (App.route.view === 'settings') { App.tiles.clear(); grid = null; Settings.render(main, App.route.section); return; }
    if (!grid) { main.innerHTML = ''; grid = h('div', { class: 'grid' }); main.append(grid); App.tiles.clear(); }
    renderDashboard();
    if (App.route.device) { const id = App.route.device; App.route.device = null; Device.openPanel(id, App.route.tab); history.replaceState(null, '', '#/'); }
    if (App.route.q && App.route.q.edit === '1' && !App.editMode) { setEditMode(true); history.replaceState(null, '', '#/'); }
  }
  function renderDashboard() {
    if (App.route.view !== 'dashboard' || !App.state.dashboard) return;
    if (!grid || !grid.isConnected) { main.innerHTML = ''; grid = h('div', { class: 'grid' }); main.append(grid); App.tiles.clear(); }
    const dash = App.state.dashboard;
    const tiles = dash.tiles || [];
    const devById = Object.fromEntries(App.state.devices.map((d) => [d.id, d]));
    const swById = Object.fromEntries((App.state.gpio.switches || []).map((s) => [s.id, s]));
    const sceneById = Object.fromEntries((dash.scenes || []).map((s) => [s.id, s]));
    const wanted = [];
    for (const t of tiles) {
      const key = t.id;
      let ent = App.tiles.get(key);
      const sig = tileSignature(t);
      if (!ent || ent.sig !== sig) { if (ent && ent.destroy) ent.destroy(); ent = buildTile(t, devById, swById, sceneById); if (!ent) continue; ent.sig = sig; App.tiles.set(key, ent); }
      ent.tile = t;
      wanted.push(ent);
    }
    // remove stale
    for (const [key, ent] of App.tiles) if (!wanted.includes(ent)) { ent.destroy && ent.destroy(); ent.el.remove(); App.tiles.delete(key); }
    // order DOM
    wanted.forEach((ent, i) => { if (grid.children[i] !== ent.el) grid.insertBefore(ent.el, grid.children[i] || null); });
    // empty state
    let empty = main.querySelector('.empty-dash');
    if (!tiles.length || tiles.every((t) => t.hidden) && !App.editMode) {
      if (!empty) { empty = el(`<div class="empty empty-dash" style="grid-column:1/-1">${icon('sparkles')}<h3>Your dashboard is empty</h3><p>Add WLED controllers and GPIO switches in Settings and they'll show up here.</p><div class="row"><a class="btn primary" href="#/settings/devices">${icon('search')} Find controllers</a><a class="btn" href="#/settings/switches">${icon('switch')} Add a switch</a></div></div>`); main.append(empty); }
    } else if (empty) empty.remove();
    updateTiles();
    if (App.editMode) grid.append(addTileButton());
    else { const b = grid.querySelector('.add-tile'); if (b) b.remove(); }
    main.classList.toggle('editing', App.editMode);
  }
  function tileSignature(t) { return [t.type, t.ref, t.size, t.hidden, t.name, t.icon, t.color, JSON.stringify(t.opts || {})].join('|'); }
  function updateTiles() {
    const devById = Object.fromEntries(App.state.devices.map((d) => [d.id, d]));
    const swById = Object.fromEntries((App.state.gpio.switches || []).map((s) => [s.id, s]));
    for (const ent of App.tiles.values()) {
      const t = ent.tile;
      if (t.type === 'device') { const d = devById[t.ref]; if (d) { d._presets = App.state.presets[d.id]; ent.update(d); } }
      else if (t.type === 'switch') { const s = swById[t.ref]; if (s) ent.update(s); }
      else if (t.type === 'all') ent.update();
    }
  }

  function tileShell(t, opts) {
    const node = h('div', { class: `tile size-${t.size || 'm'} ${t.hidden ? 'hidden-tile' : ''} ${opts.cls || ''}`, dataset: { tile: t.id } });
    if (t.color) node.style.setProperty('--tile-color', t.color);
    const head = h('div', { class: 'tile-head' });
    if (opts.icon) head.append(el(`<span class="tile-icon">${icon(opts.icon)}</span>`));
    const title = h('div', { class: 'tile-title' }, h('span', { class: 'name', text: opts.name || '' }), h('span', { class: 'sub', text: opts.sub || '' }));
    head.append(title);
    if (opts.headRight) head.append(opts.headRight);
    node.append(head);
    // edit bar
    const bar = h('div', { class: 'edit-bar' });
    const handle = el(`<span class="drag-handle btn icon sm ghost" title="Drag to move">${icon('grip')}</span>`);
    const sizes = h('div', { class: 'btn-group' });
    for (const s of ['s', 'm', 'l', 'xl']) { const b = h('button', { class: 'btn sm' + ((t.size || 'm') === s ? ' active' : ''), text: s.toUpperCase(), title: { s: 'Small', m: 'Medium', l: 'Wide', xl: 'Full width' }[s] }); b.onclick = () => updateTile(t.id, { size: s }); sizes.append(b); }
    const up = el(`<button class="btn icon sm ghost" title="Move earlier">${icon('arrowLeft')}</button>`); up.onclick = () => moveTile(t.id, -1);
    const down = el(`<button class="btn icon sm ghost" title="Move later">${icon('arrowRight')}</button>`); down.onclick = () => moveTile(t.id, 1);
    const hide = el(`<button class="btn icon sm ghost" title="${t.hidden ? 'Show' : 'Hide'}">${icon(t.hidden ? 'eye' : 'eyeOff')}</button>`); hide.onclick = () => updateTile(t.id, { hidden: !t.hidden });
    const rename = el(`<button class="btn icon sm ghost" title="Rename / style">${icon('edit')}</button>`); rename.onclick = () => tileStyleDialog(t);
    bar.append(handle, sizes, up, down, hide, rename);
    if (!['device', 'switch', 'scene'].includes(t.type)) { const del = el(`<button class="btn icon sm ghost" title="Remove tile">${icon('trash')}</button>`); del.onclick = () => api.del(`/api/dashboard/tiles/${t.id}`).then(() => refresh(true)).catch(UI.notifyError); bar.append(del); }
    node.append(bar);
    attachDrag(node, handle);
    return { node, title };
  }

  function buildTile(t, devById, swById, sceneById) {
    if (t.type === 'device') {
      const d = devById[t.ref]; if (!d) return null;
      if (!d.online && App.state.dashboard.show_offline === false && !App.editMode) return null;
      const menuBtn = el(`<button class="btn ghost icon sm" aria-label="Options">${icon('more')}</button>`);
      const shell = tileShell(t, { icon: t.icon || d.icon || 'bulb', name: t.name || d.name, sub: '', headRight: h('div', { class: 'row', style: { gap: '4px' } }, el(`<span class="status-dot ${d.online ? 'ok' : 'bad'}"></span>`), menuBtn) });
      if (!t.color && d.color) shell.node.style.setProperty('--tile-color', d.color);
      const body = Device.tileBody(d, t.size || 'm');
      shell.node.append(body);
      shell.node.addEventListener('click', (e) => { if (App.editMode) return; if (e.target.closest('button, input, .swatch, .chip, .toggle, a')) return; Device.openPanel(d.id); });
      shell.node.style.cursor = 'pointer';
      menuBtn.onclick = (e) => { e.stopPropagation(); UI.menu(menuBtn, [
        { label: 'Open controls', icon: 'sliders', onClick: () => Device.openPanel(d.id) },
        { label: 'Presets', icon: 'star', onClick: () => Device.openPanel(d.id, 'presets') },
        { label: 'Toggle power', icon: 'power', onClick: () => Device.send(d, { on: 't' }) },
        '-',
        { label: 'Edit controller', icon: 'edit', onClick: () => Device.editDevice(d, () => refresh(true)) },
      ]); };
      if (!App.state.presets[d.id] && d.online && (t.size === 'l' || t.size === 'xl')) loadPresets(d.id);
      return { el: shell.node, update: (dd) => { const dot = shell.node.querySelector('.status-dot'); dot.className = `status-dot ${dd.online ? 'ok' : 'bad'}`; dot.title = dd.online ? 'online' : (dd.last_error || 'offline'); shell.node.classList.toggle('is-on', !!(dd.online && dd.state && dd.state.on)); shell.node.classList.toggle('offline', !dd.online); shell.title.querySelector('.name').textContent = t.name || dd.name; shell.title.querySelector('.sub').textContent = dd.online ? `${dd.info && dd.info.led_count ? dd.info.led_count + ' LEDs' : ''}${dd.state && dd.state.on ? ' · ' + Math.round((dd.state.bri || 0) / 2.55) + '%' : ' · off'}` : 'offline'; body.update(dd); } };
    }
    if (t.type === 'switch') {
      const s = swById[t.ref]; if (!s) return null;
      const shell = tileShell(t, { icon: t.icon || GPIO.switchIcon(s), name: t.name || s.name, sub: `GPIO${s.pin} · ${GPIO.MODE_LABEL[s.mode] || s.mode}${s.interlock_group ? ' · interlocked' : ''}`, headRight: App.state.gpio.simulated ? el(`<span class="badge warn" title="No GPIO hardware">sim</span>`) : null });
      if (!t.color && s.color) shell.node.style.setProperty('--tile-color', s.color);
      const ctl = GPIO.control(s, { big: t.size !== 's' });
      shell.node.append(ctl);
      return { el: shell.node, update: (ss) => { shell.node.classList.toggle('is-on', !!ss.on); ctl.update(ss); shell.title.querySelector('.name').textContent = t.name || ss.name; }, destroy: () => ctl.destroy && ctl.destroy() };
    }
    if (t.type === 'scene') {
      const sc = sceneById[t.ref]; if (!sc) return null;
      const shell = tileShell(t, { icon: t.icon || sc.icon || 'sparkles', name: t.name || sc.name, sub: `${sc.actions.length} action${sc.actions.length === 1 ? '' : 's'}` });
      if (!t.color && sc.color) shell.node.style.setProperty('--tile-color', sc.color);
      const btn = el(`<button class="hold-btn">${icon('play')}<span class="label">Run scene</span><span class="progress"></span></button>`);
      btn.onclick = async () => { btn.classList.add('active'); try { const r = await api.post(`/api/scenes/${sc.id}/run`); UI.toast(r.failed.length ? 'Scene ran with problems: ' + r.failed.map((f) => f.error).join('; ') : `${sc.name} applied`, r.failed.length ? 'error' : 'success'); refresh(); } catch (e) { UI.notifyError(e); } finally { setTimeout(() => btn.classList.remove('active'), 400); } };
      shell.node.append(btn);
      return { el: shell.node, update: () => {} };
    }
    if (t.type === 'all') {
      const shell = tileShell(t, { icon: t.icon || 'sun', name: t.name || 'All lights', sub: '' });
      const power = UI.toggle(false, (on) => api.post('/api/devices/all/state', { on }).then(() => refresh()).catch(UI.notifyError), 'lg');
      const bri = UI.slider({ icon: 'sun', min: 1, max: 255, value: 128, format: (v) => Math.round(v / 2.55) + '%', onChange: (v) => api.post('/api/devices/all/state', { bri: v, on: true }).then(() => refresh()).catch(UI.notifyError) });
      const offBtn = el(`<button class="btn sm">${icon('moon')} All off</button>`); offBtn.onclick = () => api.post('/api/devices/all/state', { on: false }).then(() => refresh()).catch(UI.notifyError);
      const onBtn = el(`<button class="btn sm">${icon('sun')} All on</button>`); onBtn.onclick = () => api.post('/api/devices/all/state', { on: true }).then(() => refresh()).catch(UI.notifyError);
      shell.node.append(h('div', { class: 'tile-body' }, h('div', { class: 'row' }, power, h('div', { class: 'grow' }, bri)), h('div', { class: 'row' }, onBtn, offBtn)));
      return { el: shell.node, update: () => { const on = App.state.devices.filter((d) => d.online); const anyOn = on.some((d) => d.state && d.state.on); power.setOn(anyOn); shell.node.classList.toggle('is-on', anyOn); shell.title.querySelector('.sub').textContent = `${on.filter((d) => d.state && d.state.on).length} of ${on.length} on`; if (anyOn) { const avg = Math.round(on.filter((d) => d.state && d.state.on).reduce((a, d) => a + (d.state.bri || 0), 0) / Math.max(1, on.filter((d) => d.state && d.state.on).length)); bri.setValue(avg); } } };
    }
    if (t.type === 'clock') {
      const shell = tileShell(t, { icon: null, name: '', sub: '' }); shell.node.classList.add('clock'); shell.node.querySelector('.tile-head').remove();
      shell.node.prepend(h('div', { class: 'time', text: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }), h('div', { class: 'date', text: new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' }) }));
      return { el: shell.node, update: () => {} };
    }
    if (t.type === 'heading') {
      const shell = tileShell(t, { name: '' }); shell.node.classList.add('heading'); shell.node.querySelector('.tile-head').remove();
      shell.node.prepend(h('h2', { text: t.name || 'Section' }), (t.opts && t.opts.sub) ? h('div', { class: 'sub', text: t.opts.sub }) : null);
      return { el: shell.node, update: () => {} };
    }
    if (t.type === 'note') {
      const shell = tileShell(t, { icon: t.icon || 'note', name: t.name || 'Note' }); shell.node.classList.add('note');
      shell.node.append(h('div', { class: 'note-text', text: (t.opts && t.opts.text) || 'Double-click Edit layout → pencil to write something here.' }));
      return { el: shell.node, update: () => {} };
    }
    return null;
  }
  async function loadPresets(id) { App.state.presets[id] = []; try { App.state.presets[id] = (await api.get(`/api/devices/${id}/presets`)).presets; updateTiles(); } catch (e) {} }

  // -------------------------------------------------------- edit mode
  function setEditMode(on) { App.editMode = on; document.getElementById('topbar-actions').dataset.rendered = ''; renderTopbar(); if (on) UI.toast('Drag tiles by the handle, resize with S/M/L/XL, hide with the eye. Tap Done when finished.', 'info', 5000); renderDashboard(); }
  async function updateTile(id, patch) { try { await api.put(`/api/dashboard/tiles/${id}`, patch); await refresh(true); } catch (e) { UI.notifyError(e); } }
  async function moveTile(id, dir) { const ids = App.state.dashboard.tiles.map((t) => t.id); const i = ids.indexOf(id); const j = i + dir; if (i < 0 || j < 0 || j >= ids.length) return; ids.splice(i, 1); ids.splice(j, 0, id); try { await api.post('/api/dashboard/order', { ids }); await refresh(true); } catch (e) { UI.notifyError(e); } }
  function tileStyleDialog(t) {
    const name = h('input', { class: 'input', value: t.name || '', placeholder: t.title || 'Default name', maxlength: 60 });
    let iconName = t.icon || '', color = t.color || '';
    const body = h('div', { class: 'stack' }, UI.field('Custom name', name, 'Leave empty to use the original name'));
    if (t.type === 'heading') body.append(UI.field('Subtitle', h('input', { class: 'input', value: (t.opts && t.opts.sub) || '', id: 'tile-sub', maxlength: 80 })));
    if (t.type === 'note') body.append(UI.field('Text', h('textarea', { class: 'input', id: 'tile-text', rows: 4, text: (t.opts && t.opts.text) || '' })));
    if (!['heading', 'clock'].includes(t.type)) body.append(h('h4', { text: 'Icon' }), UI.iconPicker(iconName, (n) => { iconName = n; }), h('h4', { text: 'Colour' }), UI.colorPicker(color, (c) => { color = c; }));
    const save = el(`<button class="btn primary" data-busy="Saving…">Save</button>`);
    const m = UI.modal({ title: 'Tile style', icon: 'edit', body, footer: [save] });
    save.onclick = () => UI.busy(save, (async () => { const opts = { ...(t.opts || {}) }; const sub = body.querySelector('#tile-sub'); if (sub) opts.sub = sub.value; const txt = body.querySelector('#tile-text'); if (txt) opts.text = txt.value; await api.put(`/api/dashboard/tiles/${t.id}`, { name: name.value.trim(), icon: iconName, color, opts }); m.close(); refresh(true); })().catch(UI.notifyError));
  }
  function addTileButton() {
    let b = grid.querySelector('.add-tile');
    if (b) return b;
    b = el(`<button class="tile add-tile" style="align-items:center;justify-content:center;border-style:dashed;background:transparent;min-height:120px">${icon('plus', 'lg')}<span>Add a tile</span></button>`);
    b.onclick = () => UI.menu(b, [
      { label: 'All lights control', icon: 'sun', onClick: () => api.post('/api/dashboard/tiles', { type: 'all', size: 'm' }).then(() => refresh(true)).catch(UI.notifyError) },
      { label: 'Section heading', icon: 'type', onClick: async () => { const name = await UI.prompt({ title: 'Heading', label: 'Text', value: 'Bedroom' }); if (name) api.post('/api/dashboard/tiles', { type: 'heading', name }).then(() => refresh(true)).catch(UI.notifyError); } },
      { label: 'Clock', icon: 'clock', onClick: () => api.post('/api/dashboard/tiles', { type: 'clock', size: 'm' }).then(() => refresh(true)).catch(UI.notifyError) },
      { label: 'Note', icon: 'note', onClick: async () => { const text = await UI.prompt({ title: 'Note', label: 'Text', value: '', maxlength: 500 }); if (text) api.post('/api/dashboard/tiles', { type: 'note', name: 'Note', opts: { text } }).then(() => refresh(true)).catch(UI.notifyError); } },
      '-',
      { label: 'New scene…', icon: 'sparkles', onClick: () => Settings.sceneDialog(null, () => refresh(true)) },
      { label: 'Show hidden tiles', icon: 'eye', onClick: async () => { const hidden = App.state.dashboard.tiles.filter((t) => t.hidden); for (const t of hidden) await api.put(`/api/dashboard/tiles/${t.id}`, { hidden: false }).catch(UI.notifyError); refresh(true); } },
    ]);
    return b;
  }
  // drag & drop with pointer events (touch friendly)
  let drag = null;
  function attachDrag(node, handle) {
    handle.addEventListener('pointerdown', (e) => {
      if (!App.editMode) return;
      e.preventDefault();
      drag = { node, id: node.dataset.tile, target: null, before: false };
      node.classList.add('dragging');
      handle.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        const t = under && under.closest('.tile[data-tile]');
        grid.querySelectorAll('.drop-before, .drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
        if (!t || t === node) { drag.target = null; return; }
        const r = t.getBoundingClientRect();
        drag.before = ev.clientX < r.left + r.width / 2;
        drag.target = t; t.classList.add(drag.before ? 'drop-before' : 'drop-after');
      };
      const up = async () => {
        handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up);
        node.classList.remove('dragging');
        grid.querySelectorAll('.drop-before, .drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
        if (drag && drag.target) {
          const ids = App.state.dashboard.tiles.map((t) => t.id);
          const from = ids.indexOf(drag.id); ids.splice(from, 1);
          let to = ids.indexOf(drag.target.dataset.tile); if (!drag.before) to += 1;
          ids.splice(to, 0, drag.id);
          try { await api.post('/api/dashboard/order', { ids }); await refresh(true); } catch (err) { UI.notifyError(err); }
        }
        drag = null;
      };
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
    });
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    App.route = parseHash();
    document.getElementById('footer').innerHTML = `JimboLED ${esc(window.JIMBOLED.version)} · <a href="#/settings/system">System</a>`;
    await refresh(true);
    render();
    if (App.state.dashboard && !App.state.setupComplete && !App.state.devices.length && !(App.state.gpio.switches || []).length) {
      Wizard.run(() => refresh(true));
    }
    polling = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  }
  const style = document.createElement('style'); style.textContent = '@media (max-width: 560px) { .hide-sm { display:none } .topbar-status .pill:not(#pill-devices) { display:none } }'; document.head.append(style);
  boot();
})();
