/* Settings pages: devices, switches, scenes, appearance, security, system, backup. */
(function () {
  const { h, el, esc } = UI;
  const SECTIONS = [
    { id: 'devices', label: 'Controllers', icon: 'bulb' },
    { id: 'switches', label: 'Switches', icon: 'switch' },
    { id: 'scenes', label: 'Scenes', icon: 'sparkles' },
    { id: 'appearance', label: 'Appearance', icon: 'palette' },
    { id: 'security', label: 'Security', icon: 'lock' },
    { id: 'system', label: 'System', icon: 'cpu' },
    { id: 'backup', label: 'Backup', icon: 'box' },
  ];

  function render(main, section) {
    section = SECTIONS.some((s) => s.id === section) ? section : 'devices';
    const nav = h('nav', { class: 'settings-nav' });
    for (const s of SECTIONS) nav.append(h('a', { href: `#/settings/${s.id}`, class: s.id === section ? 'active' : '', html: icon(s.icon) + '<span>' + esc(s.label) + '</span>' }));
    const body = h('div', { class: 'settings-body' });
    main.innerHTML = '';
    main.append(h('div', { class: 'row between', style: { marginBottom: '12px' } }, h('h2', { text: 'Settings' }), el(`<a class="btn" href="#/">${icon('arrowLeft')} Back to dashboard</a>`)));
    main.append(h('div', { class: 'settings' }, nav, body));
    ({ devices: devicesSection, switches: switchesSection, scenes: scenesSection, appearance: appearanceSection, security: securitySection, system: systemSection, backup: backupSection })[section](body);
  }

  // ---------------------------------------------------------- devices
  async function devicesSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} Add by address</button>`);
    addBtn.onclick = () => Device.addDeviceDialog(() => draw());
    const findBtn = el(`<button class="btn">${icon('search')} Find controllers</button>`);
    findBtn.onclick = () => discoverDialog(() => draw());
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'WLED controllers' }), h('div', { class: 'row' }, findBtn, addBtn)), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      let devices = [];
      try { devices = (await api.get('/api/devices')).devices; } catch (e) { return UI.notifyError(e); }
      if (!devices.length) list.append(el(`<div class="empty">${icon('bulb')}<h3>No controllers yet</h3><p>Click <b>Find controllers</b> to search your network, or add one by IP address.</p></div>`));
      for (const d of devices) {
        const item = h('div', { class: 'list-item clickable' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(d.color || 'var(--accent)')}">${icon(d.icon || 'bulb')}</span>`));
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: d.name }), h('span', { class: 'sub', text: `${d.host} · ${d.online ? 'online' + (d.info && d.info.ver ? ' · WLED ' + d.info.ver : '') : 'offline' + (d.last_error ? ' – ' + d.last_error : '')}` })));
        item.append(el(`<span class="status-dot ${d.online ? 'ok' : 'bad'}"></span>`));
        const edit = el(`<button class="btn ghost icon sm" aria-label="Edit">${icon('edit')}</button>`); edit.onclick = (e) => { e.stopPropagation(); Device.editDevice(d, draw); };
        const del = el(`<button class="btn ghost icon sm" aria-label="Remove">${icon('trash')}</button>`); del.onclick = (e) => { e.stopPropagation(); Device.removeDevice(d, draw); };
        item.append(edit, del);
        item.onclick = () => Device.openPanel(d.id, 'settings');
        list.append(item);
      }
    }
    draw();
    const wled = (await api.get('/api/settings').catch(() => null));
    if (wled) {
      const poll = h('input', { class: 'input', type: 'number', min: 1, max: 120, value: wled.wled.poll_interval_s });
      const timeout = h('input', { class: 'input', type: 'number', min: 1, max: 30, value: wled.wled.request_timeout_s });
      const save = el(`<button class="btn" data-busy="Saving…">Save</button>`);
      save.onclick = () => UI.busy(save, api.put('/api/settings', { wled: { poll_interval_s: +poll.value, request_timeout_s: +timeout.value } }).then(() => UI.toast('Saved', 'success')).catch(UI.notifyError));
      body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Polling' })), el(`<p class="muted small">How often JimboLED checks each controller for changes made elsewhere (e.g. from the WLED app).</p>`), h('div', { class: 'form-grid' }, UI.field('Check every (seconds)', poll), UI.field('Give up after (seconds)', timeout)), h('div', { class: 'row end' }, save)));
    }
  }

  function discoverDialog(onDone) {
    const body = h('div', { class: 'stack' });
    const status = h('div', { class: 'row small muted' }, el(icon('refresh', 'spin')), h('span', { text: 'Searching…' }));
    const list = h('div', { class: 'list' });
    const subnet = h('input', { class: 'input mono', placeholder: 'e.g. 192.168.1.0/24', style: { maxWidth: '220px' } });
    const rescan = el(`<button class="btn sm">${icon('search')} Scan a specific network</button>`);
    rescan.onclick = () => start(subnet.value.trim() || null);
    body.append(status, list, h('details', {}, h('summary', { class: 'small muted', style: { cursor: 'pointer' }, text: 'Advanced: scan a different network range' }), h('div', { class: 'row', style: { marginTop: '8px' } }, subnet, rescan)));
    const m = UI.modal({ title: 'Find WLED controllers', icon: 'search', body, wide: true });
    let timer = null;
    async function start(net) { try { await api.post('/api/discover', net ? { subnet: net } : {}); poll(); } catch (e) { UI.notifyError(e); } }
    async function poll() {
      clearTimeout(timer);
      if (!m.el.isConnected) return;
      let s;
      try { s = await api.get('/api/discover'); } catch (e) { return UI.notifyError(e); }
      draw(s);
      if (s.running) timer = setTimeout(poll, 1200);
    }
    function draw(s) {
      status.innerHTML = s.running ? `${icon('refresh', 'spin')} <span>${esc(s.progress || 'Searching')}…</span>` : `${icon('check')} <span>Search finished – ${s.found.length} controller${s.found.length === 1 ? '' : 's'} found.</span>`;
      list.innerHTML = '';
      const all = s.found.slice();
      for (const p of s.passive || []) if (!all.some((f) => f.host === p.host)) all.push({ host: p.host, name: p.name, ver: p.vid ? 'build ' + p.vid : '', known: false, sources: ['broadcast'] });
      if (!all.length && !s.running) list.append(el(`<div class="empty">${icon('wifiOff')}<h3>Nothing found</h3><p>Make sure the controllers are powered on and connected to the same Wi‑Fi network as this Pi. You can still add one by IP address.</p></div>`));
      for (const f of all) {
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon">${icon('bulb')}</span>`));
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: f.name || f.host }), h('span', { class: 'sub', text: `${f.host}${f.ver ? ' · WLED ' + f.ver : ''}${f.led_count ? ' · ' + f.led_count + ' LEDs' : ''}` })));
        if (f.known) item.append(el(`<span class="badge ok">added</span>`));
        else { const b = el(`<button class="btn sm primary" data-busy="Adding…">${icon('plus')} Add</button>`); b.onclick = () => UI.busy(b, api.post('/api/devices', { host: f.host, name: f.name }).then(() => { UI.toast(`Added ${f.name || f.host}`, 'success'); f.known = true; draw(s); onDone && onDone(); if (window.App) App.refresh(); }).catch(UI.notifyError)); item.append(b); }
        list.append(item);
      }
      if (s.errors && s.errors.length) list.append(el(`<div class="alert warn">${esc(s.errors.join(' '))}</div>`));
    }
    start(null);
  }

  // --------------------------------------------------------- switches
  async function switchesSection(body) {
    let gpio;
    try { gpio = await api.get('/api/gpio'); } catch (e) { return UI.notifyError(e); }
    if (gpio.simulated) body.append(el(`<div class="sim-banner">${icon('alert')}<div><b>Simulation mode.</b> GPIO hardware isn't available (${esc(gpio.backend)}), so switches only pretend to work. On a Raspberry Pi with the installer this turns into real relay control.</div></div>`));
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} Add switch</button>`);
    addBtn.onclick = () => GPIO.editSwitch(null, draw);
    const bedBtn = el(`<button class="btn">${icon('bed')} Bed template</button>`);
    bedBtn.onclick = () => bedTemplate(draw);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'GPIO switches' }), h('div', { class: 'row' }, bedBtn, addBtn)), el(`<p class="muted small">Each switch drives one relay from a GPIO pin. Hold-to-run switches only stay on while the button is pressed and always time out for safety. Interlocked switches can never be on together.</p>`), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      let cfg;
      try { cfg = (await api.get('/api/gpio/switches')).switches; gpio = await api.get('/api/gpio'); } catch (e) { return UI.notifyError(e); }
      if (!cfg.length) list.append(el(`<div class="empty">${icon('switch')}<h3>No switches yet</h3><p>Add a switch for each relay you have wired to the Pi. For an adjustable bed, use the <b>Bed template</b>.</p></div>`));
      const live = Object.fromEntries((gpio.switches || []).map((s) => [s.id, s]));
      for (const s of cfg) {
        const st = live[s.id] || {};
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(s.color || 'var(--accent)')}">${icon(GPIO.switchIcon(s))}</span>`));
        const sub = `GPIO${s.pin} · ${GPIO.MODE_LABEL[s.mode]} · ${s.active_high ? 'active high' : 'active low'}${s.interlock_group ? ' · interlock "' + s.interlock_group + '"' : ''}${s.max_on_seconds ? ' · max ' + UI.fmtDuration(s.max_on_seconds) : ''}${!s.enabled ? ' · disabled' : ''}${st.error && st.error !== 'disabled' ? ' · ' + st.error : ''}`;
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name }), h('span', { class: 'sub', text: sub })));
        item.append(el(`<span class="status-dot ${st.on ? 'ok' : (st.available ? '' : 'warn')}" title="${st.on ? 'on' : 'off'}"></span>`));
        const edit = el(`<button class="btn ghost icon sm">${icon('edit')}</button>`); edit.onclick = () => GPIO.editSwitch(s, draw);
        const del = el(`<button class="btn ghost icon sm">${icon('trash')}</button>`); del.onclick = () => GPIO.removeSwitch(s, draw);
        item.append(edit, del);
        list.append(item);
      }
    }
    draw();
    // global gpio settings
    const settings = await api.get('/api/settings').catch(() => null);
    if (settings) {
      const dead = h('input', { class: 'input', type: 'number', min: 0, max: 5000, value: settings.gpio.interlock_dead_time_ms });
      const hold = h('input', { class: 'input', type: 'number', min: 0.5, max: 10, step: 0.1, value: settings.gpio.hold_timeout_s });
      const backend = h('select', { class: 'select' });
      [['auto', 'Automatic (recommended)'], ['lgpio', 'lgpio (Raspberry Pi OS Bookworm+)'], ['rpigpio', 'RPi.GPIO (older Pi OS)'], ['native', 'Native'], ['mock', 'Simulation (no hardware)']].forEach(([v, t]) => backend.append(h('option', { value: v, text: t, selected: settings.gpio.backend === v })));
      const save = el(`<button class="btn" data-busy="Saving…">Save</button>`);
      save.onclick = () => UI.busy(save, api.put('/api/settings', { gpio: { interlock_dead_time_ms: +dead.value, hold_timeout_s: +hold.value, backend: backend.value } }).then(() => { UI.toast('Saved', 'success'); draw(); }).catch(UI.notifyError));
      body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Safety settings' })), h('div', { class: 'form-grid' }, UI.field('Interlock dead time (ms)', dead, 'Pause between switching one interlocked relay off and the other on'), UI.field('Hold-to-run timeout (seconds)', hold, 'A held switch releases if the phone stops responding for this long'), UI.field('GPIO driver', backend, `Currently using: ${gpio.backend}${gpio.pi_model ? ' on ' + gpio.pi_model : ''}`)), h('div', { class: 'row end' }, save)));
      const evBtn = el(`<button class="btn">${icon('activity')} Show recent switch activity</button>`);
      evBtn.onclick = async () => { const r = await api.get('/api/gpio/events'); const log = h('div', { class: 'log' }); for (const e of r.events.slice().reverse()) log.append(h('div', { text: `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.name}: ${e.action}${e.reason ? ' (' + e.reason + ')' : ''} via ${e.source}` })); if (!r.events.length) log.textContent = 'No activity yet.'; UI.modal({ title: 'Switch activity', icon: 'activity', body: log, wide: true }); };
      body.append(h('div', { class: 'row' }, evBtn));
    }
  }
  function bedTemplate(onDone) {
    const up = h('input', { class: 'input', type: 'number', min: 2, max: 27, value: 17 });
    const down = h('input', { class: 'input', type: 'number', min: 2, max: 27, value: 27 });
    const prefix = h('input', { class: 'input', value: 'Bed', maxlength: 30 });
    const low = h('input', { type: 'checkbox', checked: true });
    const body = h('div', { class: 'stack' }, el(`<p class="muted small">Creates two <b>hold-to-run</b> switches ("Up" and "Down") that are interlocked so they can never be energised together, with a 60-second safety limit each. Use the BCM pin numbers your relay inputs are wired to.</p>`), h('div', { class: 'form-grid' }, UI.field('Name prefix', prefix), UI.field('GPIO pin for UP', up), UI.field('GPIO pin for DOWN', down)), h('label', { class: 'check' }, low, h('span', { text: 'Relay board is active LOW (typical blue relay modules)' })));
    const save = el(`<button class="btn primary" data-busy="Creating…">Create switches</button>`);
    const m = UI.modal({ title: 'Adjustable bed template', icon: 'bed', body, footer: [save] });
    save.onclick = () => UI.busy(save, (async () => {
      if (+up.value === +down.value) throw new Error('Up and down need different pins');
      const grp = (prefix.value.trim() || 'bed').toLowerCase().replace(/\s+/g, '-');
      await api.post('/api/gpio/switches', { name: `${prefix.value.trim()} up`, pin: +up.value, mode: 'momentary', active_high: !low.checked, max_on_seconds: 60, interlock_group: grp, icon: 'up' });
      await api.post('/api/gpio/switches', { name: `${prefix.value.trim()} down`, pin: +down.value, mode: 'momentary', active_high: !low.checked, max_on_seconds: 60, interlock_group: grp, icon: 'down' });
      UI.toast('Bed switches created', 'success'); m.close(); onDone && onDone(); if (window.App) App.refresh();
    })().catch(UI.notifyError));
  }

  // ----------------------------------------------------------- scenes
  async function scenesSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} New scene</button>`);
    addBtn.onclick = () => sceneDialog(null, draw);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Scenes' }), addBtn), el(`<p class="muted small">A scene is a one-tap shortcut that sends several commands at once – e.g. "Goodnight": all lights off and the bed flat.</p>`), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      const scenes = (await api.get('/api/scenes').catch(() => ({ scenes: [] }))).scenes;
      if (!scenes.length) list.append(el(`<div class="empty">${icon('sparkles')}<h3>No scenes yet</h3></div>`));
      for (const s of scenes) {
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(s.color || 'var(--accent)')}">${icon(s.icon || 'sparkles')}</span>`), h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name }), h('span', { class: 'sub', text: `${s.actions.length} action${s.actions.length === 1 ? '' : 's'}` })));
        const run = el(`<button class="btn sm">${icon('play')} Run</button>`); run.onclick = () => api.post(`/api/scenes/${s.id}/run`).then((r) => UI.toast(r.failed.length ? 'Scene ran with errors: ' + r.failed.map((f) => f.error).join('; ') : 'Scene applied', r.failed.length ? 'error' : 'success')).catch(UI.notifyError);
        const edit = el(`<button class="btn ghost icon sm">${icon('edit')}</button>`); edit.onclick = () => sceneDialog(s, draw);
        const del = el(`<button class="btn ghost icon sm">${icon('trash')}</button>`); del.onclick = async () => { if (await UI.confirm({ title: 'Delete scene', message: `Delete "${s.name}"?`, okText: 'Delete', danger: true })) { await api.del(`/api/scenes/${s.id}`).catch(UI.notifyError); draw(); if (window.App) App.refresh(); } };
        item.append(run, edit, del);
        list.append(item);
      }
    }
    draw();
  }
  async function sceneDialog(existing, onDone) {
    const [devs, sws] = await Promise.all([api.get('/api/devices').then((r) => r.devices), api.get('/api/gpio/switches').then((r) => r.switches)]);
    const scene = existing ? JSON.parse(JSON.stringify(existing)) : { name: '', icon: 'sparkles', color: '', actions: [] };
    const name = h('input', { class: 'input', value: scene.name, placeholder: 'e.g. Goodnight', maxlength: 60 });
    const list = h('div', { class: 'list' });
    function actionRow(a, i) {
      const row = h('div', { class: 'list-item', style: { flexWrap: 'wrap' } });
      const type = h('select', { class: 'select', style: { width: '150px' } });
      [['all', 'All lights'], ['device', 'One controller'], ['switch', 'Switch'], ['delay', 'Wait']].forEach(([v, t]) => type.append(h('option', { value: v, text: t, selected: a.type === v })));
      type.onchange = () => { a.type = type.value; if (a.type === 'device') { a.ref = devs[0]?.id; a.state = a.state || { on: true }; } if (a.type === 'all') { a.state = a.state || { on: true }; } if (a.type === 'switch') { a.ref = sws[0]?.id; a.action = 'off'; } if (a.type === 'delay') { a.ms = 1000; } draw(); };
      row.append(type);
      if (a.type === 'device' || a.type === 'all') {
        if (a.type === 'device') { const ref = h('select', { class: 'select', style: { width: '180px' } }); for (const d of devs) ref.append(h('option', { value: d.id, text: d.name, selected: d.id === a.ref })); ref.onchange = () => { a.ref = ref.value; }; row.append(ref); }
        const st = a.state || {};
        const on = h('select', { class: 'select', style: { width: '110px' } }); [['on', 'Turn on'], ['off', 'Turn off']].forEach(([v, t]) => on.append(h('option', { value: v, text: t, selected: (st.on === false ? 'off' : 'on') === v }))); on.onchange = () => { a.state.on = on.value === 'on'; };
        const bri = h('input', { class: 'input', type: 'number', min: 1, max: 255, value: st.bri || '', placeholder: 'brightness', style: { width: '110px' } }); bri.onchange = () => { if (bri.value) a.state.bri = +bri.value; else delete a.state.bri; };
        const ps = h('input', { class: 'input', type: 'number', min: 1, max: 250, value: st.ps || '', placeholder: 'preset #', style: { width: '100px' } }); ps.onchange = () => { if (ps.value) a.state.ps = +ps.value; else delete a.state.ps; };
        const col = h('input', { class: 'input mono', value: st.seg && st.seg.col ? Color.rgbToHex(st.seg.col[0]) : '', placeholder: '#colour', style: { width: '100px' } }); col.onchange = () => { const rgb = Color.hexToRgb(col.value); if (rgb) a.state.seg = { col: [rgb], fx: 0 }; else delete a.state.seg; };
        row.append(on, bri, ps, col);
      } else if (a.type === 'switch') {
        const ref = h('select', { class: 'select', style: { width: '180px' } }); for (const s of sws) ref.append(h('option', { value: s.id, text: s.name, selected: s.id === a.ref })); ref.onchange = () => { a.ref = ref.value; };
        const act = h('select', { class: 'select', style: { width: '120px' } }); ['off', 'on', 'toggle', 'pulse'].forEach((v) => act.append(h('option', { value: v, text: v, selected: a.action === v }))); act.onchange = () => { a.action = act.value; };
        row.append(ref, act, h('span', { class: 'small muted', text: 'hold-to-run switches only accept "off"' }));
      } else {
        const ms = h('input', { class: 'input', type: 'number', min: 0, max: 10000, step: 100, value: a.ms || 1000, style: { width: '120px' } }); ms.onchange = () => { a.ms = +ms.value; };
        row.append(ms, h('span', { class: 'small muted', text: 'milliseconds' }));
      }
      const rm = el(`<button class="btn ghost icon sm">${icon('x')}</button>`); rm.onclick = () => { scene.actions.splice(i, 1); draw(); };
      row.append(rm);
      return row;
    }
    function draw() { list.innerHTML = ''; scene.actions.forEach((a, i) => list.append(actionRow(a, i))); }
    const add = el(`<button class="btn sm">${icon('plus')} Add action</button>`); add.onclick = () => { scene.actions.push({ type: 'all', state: { on: true } }); draw(); };
    const body = h('div', { class: 'stack' }, UI.field('Scene name', name), h('h4', { text: 'Actions (run in order)' }), list, add, h('h4', { text: 'Icon' }), UI.iconPicker(scene.icon, (n) => { scene.icon = n; }), h('h4', { text: 'Colour' }), UI.colorPicker(scene.color, (c) => { scene.color = c; }));
    draw();
    const save = el(`<button class="btn primary" data-busy="Saving…">Save scene</button>`);
    const m = UI.modal({ title: existing ? 'Edit scene' : 'New scene', icon: 'sparkles', body, footer: [save], wide: true, sticky: true });
    save.onclick = () => UI.busy(save, (async () => { scene.name = name.value.trim(); if (existing) await api.put(`/api/scenes/${existing.id}`, scene); else await api.post('/api/scenes', scene); UI.toast('Scene saved', 'success'); m.close(); onDone && onDone(); if (window.App) App.refresh(); })().catch(UI.notifyError));
  }

  // ------------------------------------------------------- appearance
  async function appearanceSection(body) {
    const dash = (await api.get('/api/dashboard')).dashboard;
    const title = h('input', { class: 'input', value: dash.title, maxlength: 40 });
    const subtitle = h('input', { class: 'input', value: dash.subtitle || '', maxlength: 80, placeholder: 'e.g. Jim\'s room' });
    const themes = h('div', { class: 'chips' });
    let theme = dash.theme, accent = dash.accent, density = dash.density;
    for (const [id, label] of [['midnight', 'Midnight'], ['graphite', 'Graphite'], ['ocean', 'Ocean'], ['oled', 'Pure black (OLED)']]) { const c = h('button', { class: 'chip' + (theme === id ? ' active' : ''), text: label }); c.onclick = () => { theme = id; themes.querySelectorAll('.chip').forEach((x) => x.classList.remove('active')); c.classList.add('active'); document.documentElement.dataset.theme = id; }; themes.append(c); }
    const accentRow = h('div', { class: 'row wrap' });
    for (const c of ['#7c5cff', '#22d3ee', '#34d399', '#fbbf24', '#f97316', '#f87171', '#ec4899', '#60a5fa', '#a3e635', '#e2e8f0']) { const b = h('button', { class: 'swatch lg' + (c === accent ? ' active' : ''), style: { background: c } }); b.onclick = () => { accent = c; accentRow.querySelectorAll('.swatch').forEach((x) => x.classList.remove('active')); b.classList.add('active'); custom.value = c; document.documentElement.style.setProperty('--accent', c); }; accentRow.append(b); }
    const custom = h('input', { class: 'input mono', value: accent, maxlength: 7, style: { width: '110px' } }); custom.onchange = () => { const v = custom.value.trim().toLowerCase(); if (/^#[0-9a-f]{6}$/.test(v)) { custom.classList.remove('invalid'); accent = v; document.documentElement.style.setProperty('--accent', accent); } else { custom.classList.add('invalid'); UI.toast('Use a 6-digit hex colour like #7c5cff', 'error'); } };
    accentRow.append(custom);
    const dens = h('select', { class: 'select' }); [['comfortable', 'Comfortable'], ['compact', 'Compact']].forEach(([v, t]) => dens.append(h('option', { value: v, text: t, selected: density === v }))); dens.onchange = () => { density = dens.value; document.documentElement.dataset.density = density; };
    const showOffline = h('input', { type: 'checkbox', checked: dash.show_offline !== false });
    const showClock = h('input', { type: 'checkbox', checked: dash.show_clock !== false });
    const save = el(`<button class="btn primary" data-busy="Saving…">Save appearance</button>`);
    save.onclick = () => UI.busy(save, api.put('/api/dashboard', { title: title.value.trim(), subtitle: subtitle.value.trim(), theme, accent, density, show_offline: showOffline.checked, show_clock: showClock.checked }).then(() => { UI.toast('Saved', 'success'); if (window.App) App.refresh(true); }).catch(UI.notifyError));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Appearance' })), h('div', { class: 'form-grid' }, UI.field('Dashboard title', title), UI.field('Subtitle', subtitle)), UI.field('Theme', themes), UI.field('Accent colour', accentRow), UI.field('Density', dens), h('label', { class: 'check' }, showOffline, h('span', { text: 'Show controllers that are offline' })), h('label', { class: 'check' }, showClock, h('span', { text: 'Show the clock in the header' })), h('div', { class: 'row end' }, save)));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Dashboard layout' })), el(`<p class="muted small">Use <b>Edit layout</b> on the dashboard to drag tiles into any order, resize them, hide the ones you don't need, rename them, and add headings, notes or a clock.</p>`), h('div', { class: 'row' }, el(`<a class="btn" href="#/?edit=1">${icon('edit')} Edit the layout</a>`))));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Add to home screen' })), el(`<p class="muted small">On a phone or tablet, open this page in Safari or Chrome and choose <b>Add to Home Screen</b>. JimboLED then opens like an app, full screen.</p>`)));
  }

  // --------------------------------------------------------- security
  async function securitySection(body) {
    const s = await api.get('/api/settings');
    const current = h('input', { class: 'input', type: 'password', autocomplete: 'current-password' });
    const pw = h('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
    const pw2 = h('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
    const save = el(`<button class="btn primary" data-busy="Saving…">${s.server.password_set ? 'Change password' : 'Set password'}</button>`);
    save.onclick = () => UI.busy(save, (async () => { if (pw.value !== pw2.value) throw new Error('Passwords do not match'); await api.post('/api/settings/password', { current: current.value, password: pw.value }); UI.toast(pw.value ? 'Password set' : 'Password removed', 'success'); securitySection(body.parentNode ? (body.innerHTML = '', body) : body); })().catch(UI.notifyError));
    const remove = el(`<button class="btn danger" data-busy="Removing…">Remove password</button>`);
    remove.onclick = () => UI.busy(remove, api.post('/api/settings/password', { current: current.value, password: '' }).then(() => { UI.toast('Password removed', 'success'); body.innerHTML = ''; securitySection(body); }).catch(UI.notifyError));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Dashboard password' }), el(`<span class="badge ${s.server.password_set ? 'ok' : ''}">${s.server.password_set ? 'protected' : 'open'}</span>`)),
      el(`<p class="muted small">By default anyone on your home network can open the dashboard. Set a password if guests or children shouldn't be able to control things. WLED controllers themselves have no password, so this only protects the dashboard.</p>`),
      h('div', { class: 'form-grid' }, s.server.password_set ? UI.field('Current password', current) : null, UI.field('New password', pw, 'Leave empty to remove'), UI.field('Repeat new password', pw2)),
      h('div', { class: 'row end' }, s.server.password_set ? remove : null, save)));
    // allowed host names (DNS-rebinding guard)
    const hosts = h('input', { class: 'input mono', value: (s.server.allowed_hosts || []).join(' '), placeholder: 'e.g. jimboled.mydomain.com' });
    const saveHosts = el(`<button class="btn" data-busy="Saving…">Save names</button>`);
    saveHosts.onclick = () => UI.busy(saveHosts, api.put('/api/settings', { server: { allowed_hosts: hosts.value } }).then(() => UI.toast('Saved', 'success')).catch(UI.notifyError));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Allowed names' })), el(`<p class="muted small">For safety JimboLED only answers when opened by IP address, by a plain name, or by names ending in .local, .lan, .home and similar. If you gave the Pi a public-style DNS name, list it here (space separated).</p>`), UI.field('Extra names', hosts), h('div', { class: 'row end' }, saveHosts)));
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Good to know' })), el(`<ul class="muted small" style="margin:0;padding-left:18px;line-height:1.6"><li>Keep JimboLED on your home network. Don't forward its port on your router.</li><li>Relays are always released when JimboLED restarts, when the Pi reboots, or when a held button loses contact.</li><li>The <b>All off</b> button in the header turns off every relay and every light immediately.</li></ul>`)));
  }

  // ----------------------------------------------------------- system
  async function systemSection(body) {
    const s = await api.get('/api/system');
    const stats = h('div', { class: 'stats' });
    const stat = (label, value) => stats.append(h('div', { class: 'stat' }, h('span', { class: 'label', text: label }), h('span', { class: 'value', text: value })));
    stat('Version', s.version + (s.git && s.git.rev ? ` (${s.git.rev})` : ''));
    stat('Device', s.pi_model || s.platform.split('-')[0]);
    stat('Uptime', UI.fmtDuration(s.uptime_s));
    if (s.cpu_temp_c != null) stat('CPU temp', s.cpu_temp_c.toFixed(0) + ' °C');
    if (s.memory && s.memory.total) stat('Memory free', UI.fmtBytes(s.memory.available));
    stat('Disk free', UI.fmtBytes(s.disk.free));
    stat('GPIO driver', s.gpio_backend + (s.gpio_simulated ? ' (simulated)' : ''));
    const addr = s.addresses.map((a) => `http://${a}${s.port && s.port !== 80 ? ':' + s.port : ''}`);
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'This Pi' })), stats, el(`<p class="muted small">Open the dashboard from any device on your network at <b>http://${esc(s.hostname)}.local${s.port && s.port !== 80 ? ':' + s.port : ''}</b>${addr.length ? ' or ' + addr.map((a) => '<b>' + esc(a) + '</b>').join(', ') : ''}.</p>`)));
    const card = h('div', { class: 'card' });
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Updates & maintenance' })));
    if (!s.helper) card.append(el(`<div class="alert info">Updates, restart and reboot buttons need JimboLED to be installed with <code>install.sh</code> on the Pi. Running from a plain checkout, use the command line instead.</div>`));
    const check = el(`<button class="btn" data-busy="Checking…" ${s.helper ? '' : 'disabled'}>${icon('refresh')} Check for updates</button>`);
    const out = h('div', { class: 'small muted' });
    check.onclick = () => UI.busy(check, api.post('/api/system/update/check').then((r) => { if (r.update_available) { out.innerHTML = `${icon('sparkles')} A new version is available (${esc(r.remote || '')}). `; const b = el(`<button class="btn sm primary" data-busy="Updating (this takes a few minutes)…">Update now</button>`); b.onclick = () => UI.busy(b, api.post('/api/system/update').then((r2) => { UI.toast(r2.message, 'success', 8000); out.innerHTML = `${icon('refresh', 'spin')} Updating… this takes a few minutes on a Pi Zero. The page reloads automatically when JimboLED is back.`; const startRev = r2.rev; let tries = 0; const poll = setInterval(async () => { tries++; try { const sys = await api.get('/api/system'); if ((sys.git && sys.git.rev && sys.git.rev !== startRev) || tries > 120) { clearInterval(poll); location.reload(); } } catch (e) { /* restarting */ } }, 5000); }).catch(UI.notifyError)); out.append(b); } else out.textContent = r.message || 'You are up to date.'; }).catch(UI.notifyError));
    const restart = el(`<button class="btn" data-busy="Restarting…" ${s.helper ? '' : 'disabled'}>${icon('rotate')} Restart JimboLED</button>`);
    restart.onclick = async () => { if (await UI.confirm({ title: 'Restart JimboLED', message: 'All relays will be released. The dashboard reconnects in a few seconds.', okText: 'Restart' })) UI.busy(restart, api.post('/api/system/restart').then((r) => { UI.toast(r.message, 'success'); setTimeout(() => location.reload(), 6000); }).catch(UI.notifyError)); };
    const reboot = el(`<button class="btn danger" data-busy="Rebooting…" ${s.helper ? '' : 'disabled'}>${icon('power')} Reboot the Pi</button>`);
    reboot.onclick = async () => { if (await UI.confirm({ title: 'Reboot the Raspberry Pi', message: 'The Pi will be unavailable for about a minute.', okText: 'Reboot', danger: true })) UI.busy(reboot, api.post('/api/system/reboot').then((r) => UI.toast(r.message, 'success', 10000)).catch(UI.notifyError)); };
    card.append(h('div', { class: 'row wrap' }, check, restart, reboot), out);
    body.append(card);
    const logCard = h('div', { class: 'card' });
    const log = h('div', { class: 'log' });
    const refreshLogs = async () => { const r = await api.get('/api/system/logs?limit=300'); log.innerHTML = ''; for (const l of r.logs) log.append(h('div', { class: l.level, text: `${new Date(l.ts * 1000).toLocaleTimeString()} ${l.level.padEnd(7)} ${l.msg}` })); log.scrollTop = log.scrollHeight; if (!r.logs.length) log.textContent = 'No log entries yet.'; };
    const rl = el(`<button class="btn sm">${icon('refresh')} Refresh</button>`); rl.onclick = refreshLogs;
    logCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Log' }), rl), log);
    body.append(logCard); refreshLogs();
  }

  // ----------------------------------------------------------- backup
  async function backupSection(body) {
    const dl = el(`<a class="btn primary" href="/api/backup" download>${icon('download')} Download backup</a>`);
    const file = h('input', { type: 'file', accept: 'application/json,.json', class: 'input' });
    const up = el(`<button class="btn" data-busy="Restoring…">${icon('upload')} Restore from file</button>`);
    up.onclick = () => { if (!file.files[0]) return UI.toast('Choose a backup file first', 'error'); UI.confirm({ title: 'Restore backup', message: 'This replaces all controllers, switches and dashboard settings with the ones in the file.', okText: 'Restore' }).then((ok) => { if (!ok) return; const fd = new FormData(); fd.append('file', file.files[0]); UI.busy(up, api.upload('/api/restore', fd).then(() => { UI.toast('Backup restored', 'success'); if (window.App) App.refresh(true); draw(); }).catch(UI.notifyError)); }); };
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Backup & restore' })), el(`<p class="muted small">Everything JimboLED knows – controllers, switches, scenes and your dashboard layout – lives in one file. Download it after you finish setting things up. Presets live on the WLED controllers themselves and are not included.</p>`), h('div', { class: 'row wrap' }, dl), h('div', { class: 'row wrap' }, file, up)));
    const card = h('div', { class: 'card' }); const list = h('div', { class: 'list' });
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Automatic snapshots' })), el(`<p class="muted small">JimboLED keeps a snapshot every time you change something important, so you can undo mistakes.</p>`), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      const r = await api.get('/api/backups');
      if (!r.backups.length) list.append(h('div', { class: 'muted small', text: 'No snapshots yet.' }));
      for (const b of r.backups.slice(0, 15)) { const item = h('div', { class: 'list-item' }); const reason = b.name.replace(/^config-\d{8}-\d{6}-/, '').replace(/\.json$/, ''); item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: new Date(b.mtime * 1000).toLocaleString() }), h('span', { class: 'sub', text: `before: ${reason}` }))); const rb = el(`<button class="btn sm" data-busy="Restoring…">Restore</button>`); rb.onclick = async () => { if (await UI.confirm({ title: 'Restore snapshot', message: `Go back to the configuration from ${new Date(b.mtime * 1000).toLocaleString()}?`, okText: 'Restore' })) UI.busy(rb, api.post(`/api/backups/${b.name}/restore`).then(() => { UI.toast('Snapshot restored', 'success'); if (window.App) App.refresh(true); }).catch(UI.notifyError)); }; item.append(rb); list.append(item); }
    }
    draw();
  }

  window.Settings = { render, discoverDialog, bedTemplate, sceneDialog, SECTIONS };
})();
