/* Settings pages: devices, switches, scenes, appearance, security, system, backup. */
(function () {
  const { h, el, esc } = UI;
  const SECTIONS = [
    { id: 'devices', label: 'Controllers', icon: 'bulb' },
    { id: 'switches', label: 'Switches', icon: 'switch' },
    { id: 'scenes', label: 'Scenes', icon: 'sparkles' },
    { id: 'estop', label: 'Emergency stop', icon: 'estop' },
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
    ({ devices: devicesSection, switches: switchesSection, scenes: scenesSection, estop: estopSection, appearance: appearanceSection, security: securitySection, system: systemSection, backup: backupSection })[section](body);
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


  // ---------------------------------------------------- emergency stop
  const SCOPE_LABEL = { all: 'Every relay', group: 'An interlock group', switch: 'Chosen switches' };

  /** A <select> of usable BCM pins, annotated with what already owns them. */
  async function pinSelect(current, skipId) {
    const sel = h('select', { class: 'select' });
    sel.append(h('option', { value: '', text: 'Choose a GPIO pin…', disabled: true, selected: current == null }));
    const { pins } = await api.get('/api/gpio/pins');
    for (const p of pins) {
      if (p.reserved) continue;
      const taken = p.in_use_by && p.bcm !== current;
      sel.append(h('option', {
        value: String(p.bcm), disabled: !!taken, selected: p.bcm === current,
        text: `GPIO${p.bcm} (pin #${p.physical})` + (taken ? ` — used by ${p.in_use_by}` : p.recommended ? ' — recommended' : ''),
      }));
    }
    return sel;
  }

  function zoneDialog(existing, switches, onSaved) {
    const z = Object.assign({ name: '', scope: 'group', refs: [], icon: 'estop', color: '' }, existing || {});
    const name = h('input', { class: 'input', value: z.name, maxlength: 60, placeholder: 'e.g. Bed' });
    const scope = h('select', { class: 'select' });
    for (const v of ['group', 'switch']) scope.append(h('option', { value: v, text: SCOPE_LABEL[v], selected: z.scope === v }));
    const refsWrap = h('div', { class: 'stack' });
    let refs = new Set(z.refs || []);
    function drawRefs() {
      refsWrap.innerHTML = '';
      const chips = h('div', { class: 'chips' });
      const options = scope.value === 'group'
        ? [...new Set(switches.map((s) => s.interlock_group).filter(Boolean))].map((g) => [g, g])
        : switches.map((s) => [s.id, s.name]);
      if (!options.length) {
        refsWrap.append(el(`<div class="alert warn">${scope.value === 'group'
          ? 'No interlock groups yet. Give the bed\'s up/down switches the same interlock group first (Settings → Switches).'
          : 'No switches yet — add one under Settings → Switches.'}</div>`));
        return;
      }
      for (const [value, label] of options) {
        const c = h('button', { type: 'button', class: 'chip' + (refs.has(value) ? ' active' : ''), text: label });
        c.onclick = () => { refs.has(value) ? refs.delete(value) : refs.add(value); c.classList.toggle('active', refs.has(value)); };
        chips.append(c);
      }
      refsWrap.append(chips);
    }
    scope.onchange = () => { refs = new Set(); drawRefs(); };
    drawRefs();
    let iconName = z.icon, color = z.color;
    const body = h('div', { class: 'stack' },
      UI.field('Name', name, 'What this stop is called on the button and in the log.'),
      UI.field('Covers', scope),
      UI.field('Which ones', refsWrap, 'Tap to include. Anything not covered keeps working normally.'),
      h('h4', { text: 'Appearance' }),
      UI.iconPicker(iconName || 'estop', (n) => { iconName = n; }),
      UI.colorPicker(color, (c) => { color = c; }));
    const cancel = el(`<button class="btn">Cancel</button>`);
    const save = el(`<button class="btn primary" data-busy="Saving…">${existing ? 'Save changes' : 'Add stop'}</button>`);
    const m = UI.modal({ title: existing ? 'Edit emergency stop' : 'Add an emergency stop', icon: 'estop', body, footer: [cancel, save], wide: true, sticky: true });
    cancel.onclick = m.close;
    save.onclick = () => UI.busy(save, (async () => {
      const payload = { name: name.value.trim(), scope: scope.value, refs: [...refs], icon: iconName, color };
      if (!payload.name) throw new Error('Give this stop a name');
      if (!payload.refs.length) throw new Error('Pick at least one switch or group for this stop to cover');
      if (existing) await api.put(`/api/estop/zones/${existing.id}`, payload);
      else await api.post('/api/estop/zones', payload);
      UI.toast('Saved', 'success'); m.close(); onSaved && onSaved();
    })().catch(UI.notifyError));
  }

  async function inputDialog(existing, zones, onSaved) {
    const i = Object.assign({ name: 'Emergency stop button', pin: null, zone: 'all', normally_closed: true, pull: 'up', enabled: true }, existing || {});
    const name = h('input', { class: 'input', value: i.name, maxlength: 60 });
    const pin = await pinSelect(existing ? i.pin : null);
    const zone = h('select', { class: 'select' });
    for (const z of zones) zone.append(h('option', { value: z.id, text: z.name, selected: z.id === i.zone }));
    const wiring = h('select', { class: 'select' },
      h('option', { value: 'nc', text: 'Normally closed (recommended) — a press or a broken wire stops everything', selected: i.normally_closed }),
      h('option', { value: 'no', text: 'Normally open — only an actual press stops it', selected: !i.normally_closed }));
    const pull = h('select', { class: 'select' },
      h('option', { value: 'up', text: 'Pull-up — wire the button between the pin and GND', selected: i.pull === 'up' }),
      h('option', { value: 'down', text: 'Pull-down — wire the button between the pin and 3.3 V', selected: i.pull === 'down' }));
    const enabled = h('input', { type: 'checkbox', checked: i.enabled !== false });
    const body = h('div', { class: 'stack' },
      el(`<div class="alert info">Wire a <b>normally-closed</b> mushroom button between your chosen GPIO pin and a ground pin. Leave the pull-up on. The closed button holds the pin at 0 V; pressing it — or cutting the cable — breaks the loop and stops the relays within about a fifth of a second.</div>`),
      h('div', { class: 'form-grid' },
        UI.field('Name', name),
        UI.field('Stops', zone, 'Which emergency stop this button engages.'),
        h('div', { class: 'full' }, UI.field('GPIO pin', pin)),
        h('div', { class: 'full' }, UI.field('Button type', wiring)),
        h('div', { class: 'full' }, UI.field('Resistor', pull))),
      h('label', { class: 'check' }, enabled, h('span', { text: 'Enabled' })));
    const cancel = el(`<button class="btn">Cancel</button>`);
    const save = el(`<button class="btn primary" data-busy="Saving…">${existing ? 'Save changes' : 'Add button'}</button>`);
    const m = UI.modal({ title: existing ? 'Edit emergency stop button' : 'Add a physical emergency stop', icon: 'estop', body, footer: [cancel, save], wide: true, sticky: true });
    cancel.onclick = m.close;
    save.onclick = () => UI.busy(save, (async () => {
      if (!pin.value) throw new Error('Pick a GPIO pin');
      const payload = { name: name.value.trim() || 'Emergency stop', pin: +pin.value, zone: zone.value,
                        normally_closed: wiring.value === 'nc', pull: pull.value, enabled: enabled.checked };
      if (existing) await api.put(`/api/estop/inputs/${existing.id}`, payload);
      else await api.post('/api/estop/inputs', payload);
      UI.toast('Saved', 'success'); m.close(); onSaved && onSaved();
    })().catch(UI.notifyError));
  }

  async function estopSection(body) {
    const zonesCard = h('div', { class: 'card' });
    const inputsCard = h('div', { class: 'card' });
    body.append(
      el(`<div class="alert info"><b>All off</b> switches everything off — and anything can be switched straight back on. An <b>emergency stop</b> latches: it cuts its relays and keeps them locked out, from every phone and every scene, until somebody resets it here.</div>`),
      zonesCard, inputsCard,
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, h('h3', { text: 'Wiring a physical button' })),
        el(`<p class="muted small">See <a href="https://github.com/SethMorrowSoftware/JimboLED/blob/main/docs/WIRING.md" target="_blank" rel="noopener">docs/WIRING.md</a> for the full guide, including which pins are safe during power-up.</p>`)));

    async function draw() {
      const [estop, gpio] = await Promise.all([api.get('/api/estop'), api.get('/api/gpio')]);
      const switches = gpio.switches || [];

      // ---- zones
      zonesCard.innerHTML = '';
      const addZone = el(`<button class="btn primary">${icon('plus')} Add a stop</button>`);
      addZone.onclick = () => zoneDialog(null, switches, draw);
      zonesCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Emergency stops' }), addZone));
      const list = h('div', { class: 'list' });
      for (const z of estop.zones) {
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(z.color || 'var(--danger)')}">${icon(z.engaged ? 'lock' : (z.icon || 'estop'))}</span>`));
        const covered = (z.switches || []).map((id) => (switches.find((s) => s.id === id) || {}).name).filter(Boolean);
        item.append(h('div', { class: 'grow' },
          h('span', { class: 'title', text: z.name }),
          h('span', { class: 'sub', text: (z.builtin ? 'Built in · ' : '') + (covered.length ? `covers ${covered.join(', ')}` : 'covers nothing yet') })));
        if (z.engaged) item.append(el(`<span class="badge danger">engaged</span>`));
        if (!z.builtin) {
          const edit = el(`<button class="btn icon sm ghost" title="Edit">${icon('edit')}</button>`);
          edit.onclick = () => zoneDialog(z, switches, draw);
          const del = el(`<button class="btn icon sm ghost" title="Remove">${icon('trash')}</button>`);
          del.onclick = async () => {
            if (!await UI.confirm({ title: 'Remove stop', message: `Remove “${z.name}”? Its relays go back to being covered only by the master stop.`, okText: 'Remove', danger: true })) return;
            try { await api.del(`/api/estop/zones/${z.id}`); UI.toast('Removed', 'success'); draw(); } catch (e) { UI.notifyError(e); }
          };
          item.append(edit, del);
        }
        list.append(item);
      }
      zonesCard.append(list);
      if (estop.zones.length === 1) {
        zonesCard.append(el(`<p class="muted small">Right now one button stops everything. Add a stop per moving thing — <b>Bed</b>, <b>Awning</b> — so stopping one does not lock out the other.</p>`));
      }

      // ---- hardware inputs
      inputsCard.innerHTML = '';
      const addInput = el(`<button class="btn">${icon('plus')} Add a button</button>`);
      addInput.onclick = () => inputDialog(null, estop.zones, draw);
      inputsCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Physical buttons' }), addInput));
      if (!estop.inputs.length) {
        inputsCard.append(el(`<p class="muted small">None wired. The on-screen stop works on its own — but a real button beside the bed works when the phone is asleep, out of reach or out of battery.</p>`));
      }
      const ilist = h('div', { class: 'list' });
      for (const i of estop.inputs) {
        const item = h('div', { class: 'list-item' });
        const bad = !!i.error || i.tripped;
        item.append(el(`<span class="status-dot ${bad ? 'bad' : 'ok'}"></span>`));
        const zoneName = (estop.zones.find((z) => z.id === i.zone) || {}).name || i.zone;
        const detail = i.error ? i.error : i.tripped ? 'pressed / circuit open' : 'healthy';
        item.append(h('div', { class: 'grow' },
          h('span', { class: 'title', text: i.name }),
          h('span', { class: 'sub', text: `GPIO${i.pin} · ${i.normally_closed ? 'normally closed' : 'normally open'} · stops “${zoneName}” · ${detail}` })));
        if (i.simulated) item.append(el(`<span class="badge warn" title="No GPIO hardware: this button is not read">sim</span>`));
        const edit = el(`<button class="btn icon sm ghost" title="Edit">${icon('edit')}</button>`);
        edit.onclick = () => inputDialog(i, estop.zones, draw);
        const del = el(`<button class="btn icon sm ghost" title="Remove">${icon('trash')}</button>`);
        del.onclick = async () => {
          if (!await UI.confirm({ title: 'Remove button', message: `Remove “${i.name}”? The relays lose this physical stop.`, okText: 'Remove', danger: true })) return;
          try { await api.del(`/api/estop/inputs/${i.id}`); UI.toast('Removed', 'success'); draw(); } catch (e) { UI.notifyError(e); }
        };
        item.append(edit, del);
        ilist.append(item);
      }
      inputsCard.append(ilist);
    }
    draw().catch(UI.notifyError);
  }

  // ------------------------------------------------------- appearance
  const THEME_GROUPS = [
    ['Follow my device', [['auto', 'Auto (light / dark)']]],
    ['Dark', [['midnight', 'Midnight'], ['graphite', 'Graphite'], ['ocean', 'Ocean'], ['oled', 'Pure black (OLED)']]],
    ['Light', [['daylight', 'Daylight'], ['paper', 'Paper']]],
  ];
  const ACCENTS = ['#7c5cff', '#22d3ee', '#34d399', '#fbbf24', '#f97316', '#f87171', '#ec4899', '#60a5fa', '#a3e635', '#e2e8f0'];

  /** A row of chips bound to one value, previewed live on <html>. */
  function chipGroup(options, current, onPick) {
    const wrap = h('div', { class: 'chips' });
    for (const [value, label] of options) {
      const c = h('button', { type: 'button', class: 'chip' + (current === value ? ' active' : ''), text: label });
      c.onclick = () => {
        wrap.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        onPick(value);
      };
      wrap.append(c);
    }
    return wrap;
  }

  async function appearanceSection(body) {
    const dash = (await api.get('/api/dashboard')).dashboard;
    const root = document.documentElement;
    const live = { theme: dash.theme, accent: dash.accent, density: dash.density,
                   radius: dash.radius || 'soft', text_scale: dash.text_scale || 100 };
    // Everything previews on the real page as you pick it, and is put back if
    // you navigate away without saving.
    const original = { ...live };
    const apply = () => {
      root.dataset.theme = live.theme;
      root.dataset.density = live.density;
      root.dataset.radius = live.radius;
      root.style.setProperty('--accent', live.accent);
      root.style.setProperty('--text-scale', (live.text_scale / 100).toFixed(3));
    };

    const title = h('input', { class: 'input', value: dash.title, maxlength: 40 });
    const subtitle = h('input', { class: 'input', value: dash.subtitle || '', maxlength: 80, placeholder: 'e.g. Jim\'s room' });

    const themeWrap = h('div', { class: 'stack' });
    for (const [groupName, opts] of THEME_GROUPS) {
      const chips = chipGroup(opts, live.theme, (v) => {
        live.theme = v; apply();
        themeWrap.querySelectorAll('.chip').forEach((x) => x.classList.toggle('active', x.dataset.theme === v));
      });
      // 'theme-swatch' opts each chip into rendering with its own palette,
      // so the choice looks like what it will do.
      chips.querySelectorAll('.chip').forEach((c, i) => { c.dataset.theme = opts[i][0]; c.classList.add('theme-swatch'); });
      themeWrap.append(h('div', { class: 'stack', style: { gap: '4px' } },
        h('span', { class: 'hint', text: groupName }), chips));
    }

    const accentRow = h('div', { class: 'row wrap' });
    const markAccent = () => accentRow.querySelectorAll('.swatch').forEach((x) => x.classList.toggle('active', x.dataset.color === live.accent));
    for (const c of ACCENTS) {
      const b = h('button', { type: 'button', class: 'swatch lg', style: { background: c }, dataset: { color: c },
                              title: c, 'aria-label': `Accent colour ${c}` });
      b.onclick = () => { live.accent = c; custom.value = c; custom.classList.remove('invalid'); apply(); markAccent(); };
      accentRow.append(b);
    }
    const custom = h('input', { class: 'input mono', value: live.accent, maxlength: 7, style: { width: '110px' },
                                'aria-label': 'Custom accent colour, hex' });
    custom.onchange = () => {
      const v = custom.value.trim().toLowerCase();
      if (/^#[0-9a-f]{6}$/.test(v)) { custom.classList.remove('invalid'); live.accent = v; apply(); markAccent(); }
      else { custom.classList.add('invalid'); UI.toast('Use a 6-digit hex colour like #7c5cff', 'error'); }
    };
    accentRow.append(custom);
    markAccent();

    const density = chipGroup([['comfortable', 'Comfortable'], ['compact', 'Compact'], ['roomy', 'Roomy']],
      live.density, (v) => { live.density = v; apply(); });
    const radius = chipGroup([['sharp', 'Sharp'], ['soft', 'Soft'], ['round', 'Round']],
      live.radius, (v) => { live.radius = v; apply(); });

    const scale = UI.slider({
      icon: 'textSize', min: 85, max: 150, step: 5, value: live.text_scale,
      format: (v) => `${v}%`,
      onInput: (v) => { live.text_scale = +v; apply(); },
      onChange: (v) => { live.text_scale = +v; apply(); },
    });

    const showOffline = h('input', { type: 'checkbox', checked: dash.show_offline !== false });
    const showClock = h('input', { type: 'checkbox', checked: dash.show_clock !== false });
    const showEstop = h('input', { type: 'checkbox', checked: dash.show_estop !== false });

    const save = el(`<button class="btn primary" data-busy="Saving…">Save appearance</button>`);
    save.onclick = () => UI.busy(save, api.put('/api/dashboard', {
      title: title.value.trim(), subtitle: subtitle.value.trim(),
      theme: live.theme, accent: live.accent, density: live.density,
      radius: live.radius, text_scale: live.text_scale,
      show_offline: showOffline.checked, show_clock: showClock.checked, show_estop: showEstop.checked,
    }).then(() => { Object.assign(original, live); UI.toast('Saved', 'success'); if (window.App) App.refresh(true); }).catch(UI.notifyError));

    const revert = el(`<button class="btn ghost">Undo changes</button>`);
    revert.onclick = () => { Object.assign(live, original); apply(); render(document.getElementById('main'), 'appearance'); };

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Appearance' }),
        h('span', { class: 'hint', text: 'Previewed live — nothing is kept until you save' })),
      h('div', { class: 'form-grid' }, UI.field('Dashboard title', title), UI.field('Subtitle', subtitle)),
      UI.field('Theme', themeWrap, 'Auto follows your phone or computer between light and dark.'),
      UI.field('Accent colour', accentRow),
      UI.field('Spacing', density, 'How much room each tile gets.'),
      UI.field('Corners', radius),
      UI.field('Text size', scale, 'Larger text for reading the dashboard at arm\'s length.'),
      h('label', { class: 'check' }, showOffline, h('span', { text: 'Show controllers that are offline' })),
      h('label', { class: 'check' }, showClock, h('span', { text: 'Show the clock in the header' })),
      h('label', { class: 'check' }, showEstop, h('span', { text: 'Show the emergency stop button in the header' })),
      h('div', { class: 'row end' }, revert, save)));

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
