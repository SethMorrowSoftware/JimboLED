/* Settings pages: devices, groups, switches, scenes, schedules, appearance,
   security, integrations, system, backup.  Groups, schedules and
   integrations live in automation.js. */
(function () {
  const { h, el, esc } = UI;
  const SECTIONS = [
    { id: 'home', label: 'All settings', icon: 'grid' },
    { id: 'devices', label: 'Light controllers', icon: 'bulb', desc: 'Find and add your WLED lights, rename them, and check they are online.' },
    { id: 'groups', label: 'Light groups', icon: 'home', desc: 'Put lights together, like "Bedroom", to control them with one tile.' },
    { id: 'switches', label: 'Relays & switches', icon: 'switch', desc: 'Buttons for the bed motor, lamps, fans and anything else on a relay.' },
    { id: 'scenes', label: 'Scenes', icon: 'sparkles', desc: 'One tap for several things at once, like "Goodnight".' },
    { id: 'schedules', label: 'Schedules', icon: 'clock', desc: 'Make things happen by themselves at set times or at sunset.' },
    { id: 'estop', label: 'Emergency stop', icon: 'estop', desc: 'The big red button that stops motors and keeps them stopped.' },
    { id: 'appearance', label: 'Appearance', icon: 'palette', desc: 'Your name, logo, colours, background and wall tablet mode.' },
    { id: 'security', label: 'Password', icon: 'lock', desc: 'Keep guests and children from changing things.' },
    { id: 'integrations', label: 'Other apps', icon: 'key', desc: 'Phone notifications, Home Assistant and Shortcuts.' },
    { id: 'system', label: 'System', icon: 'cpu', desc: 'Updates, restarting, and how the Raspberry Pi is doing.' },
    { id: 'backup', label: 'Backup', icon: 'box', desc: 'Save everything to a file, or undo a recent change.' },
  ];

  // What each page is for, in plain words, shown at the top of it.
  const INTROS = {
    devices: ['bulb', 'Light controllers', 'Each <b>WLED controller</b> is a small box that runs one LED strip or light. Press <b>Find controllers</b> and JimboLED searches your Wi‑Fi for them. Tap one to rename it or change its settings.'],
    groups: ['home', 'Light groups', 'A group puts several lights under one tile, so <b>Bedroom</b> can turn the bed strip and the ceiling light on together. A light can be in more than one group.'],
    scenes: ['sparkles', 'Scenes', 'A scene is one button that does several things at once, for example <b>Goodnight</b>: lights off and the bed flat. The easiest way to make one: set the lights how you like them, then press <b>New scene</b> and <b>Capture the lights as they are now</b>.'],
    schedules: ['clock', 'Schedules', 'Make things happen on their own: porch lights at sunset, a wake-up light at 6:45 on weekdays. They run on the Raspberry Pi, so nobody needs to have the dashboard open.'],
    estop: ['estop', 'Emergency stop', '<b>All off</b> turns everything off, and anything can be turned straight back on. An <b>emergency stop</b> is stronger: it cuts its relays and keeps them locked off, from every phone and every scene, until somebody resets it here.'],
    appearance: ['palette', 'Appearance', 'Make the dashboard yours: your name, logo, colours and background. Everything previews straight away, and nothing is kept until you press <b>Save appearance</b>.'],
    security: ['lock', 'Password', 'Anyone on your home Wi‑Fi can open the dashboard until you set a password here. Then everyone signs in once on each phone or tablet.'],
    integrations: ['key', 'Other apps', 'Get a message on your phone when something needs attention, and let other apps such as Home Assistant or iPhone Shortcuts control JimboLED. You don\'t need any of this for everyday use.'],
    system: ['cpu', 'System', 'How the Raspberry Pi is doing, how to reach the dashboard from another device, and buttons to update or restart.'],
    backup: ['box', 'Backup', 'Save all of your settings, including your logo, to one file you can keep safe. JimboLED also saves a snapshot by itself each time you change something important, so you can undo a mistake.'],
  };

  function render(main, section) {
    section = SECTIONS.some((s) => s.id === section) ? section : 'home';
    const nav = h('nav', { class: 'settings-nav', 'aria-label': 'Settings pages' });
    for (const s of SECTIONS) nav.append(h('a', { href: `#/settings/${s.id}`, class: s.id === section ? 'active' : '', title: s.desc || '', html: icon(s.icon) + '<span>' + esc(s.label) + '</span>' }));
    const body = h('div', { class: 'settings-body' });
    main.innerHTML = '';
    main.append(h('div', { class: 'row between', style: { marginBottom: '12px' } }, h('h2', { text: 'Settings' }), el(`<a class="btn" href="#/">${icon('arrowLeft')} Back to dashboard</a>`)));
    main.append(h('div', { class: 'settings' }, nav, body));
    if (INTROS[section]) body.append(UI.intro(...INTROS[section]));
    ({ home: homeSection, devices: devicesSection, groups: Automation.groupsSection, switches: switchesSection, scenes: scenesSection, schedules: Automation.schedulesSection, estop: estopSection, appearance: appearanceSection, security: securitySection, integrations: Automation.integrationsSection, system: systemSection, backup: backupSection })[section](body);
  }

  /** The front page of Settings: every page, with one line on what it is for. */
  function homeSection(body) {
    body.append(UI.intro('settings', 'What would you like to change?', 'Pick a topic. Every page explains itself at the top, and anything technical is folded away under <b>Advanced</b>.'));
    const grid = h('div', { class: 'settings-home' });
    for (const s of SECTIONS) {
      if (!s.desc) continue;
      grid.append(h('a', { href: `#/settings/${s.id}` }, h('span', { class: 'tile-icon', html: icon(s.icon) }), h('span', { class: 'grow' }, h('b', { text: s.label }), h('span', { class: 'small', text: s.desc }))));
    }
    body.append(grid);
  }

  // ---------------------------------------------------------- devices
  async function devicesSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} Add by address</button>`);
    addBtn.onclick = () => Device.addDeviceDialog(() => draw());
    const findBtn = el(`<button class="btn">${icon('search')} Find controllers</button>`);
    findBtn.onclick = () => discoverDialog(() => draw());
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Your lights' }), h('div', { class: 'row' }, findBtn, addBtn)), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      let devices = [];
      try { devices = (await api.get('/api/devices')).devices; } catch (e) { return UI.notifyError(e); }
      if (!devices.length) list.append(el(`<div class="empty">${icon('bulb')}<h3>No controllers yet</h3><p>Click <b>Find controllers</b> to search your network, or add one by IP address.</p></div>`));
      for (const d of devices) {
        const item = h('div', { class: 'list-item clickable' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(d.color || 'var(--accent)')}">${icon(d.icon || 'bulb')}</span>`));
        const inf = d.info || {};
        const health = d.online ? [
          'online', inf.ver ? 'WLED ' + inf.ver : '', inf.led_count ? inf.led_count + ' LEDs' : '',
          inf.signal != null ? `Wi-Fi ${inf.signal}%${inf.signal < 40 ? ' (weak)' : ''}` : '',
          inf.power_ma ? `${(inf.power_ma / 1000).toFixed(1)} A` : '', inf.uptime ? 'up ' + UI.fmtDuration(inf.uptime) : '',
        ].filter(Boolean).join(' · ') : (d.enabled === false ? 'disabled' : 'offline' + (d.last_error ? ' – ' + d.last_error : ''));
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: d.name }), h('span', { class: 'sub', text: `${d.host} · ${health}` })));
        item.append(el(`<span class="status-dot ${d.online ? 'ok' : 'bad'}"></span>`));
        const flash = el(`<button class="btn ghost icon sm" aria-label="Flash it" title="Flash it, to see which light it is" ${d.online ? '' : 'disabled'}>${icon('zap')}</button>`); flash.onclick = (e) => { e.stopPropagation(); Device.identify(d); };
        const edit = el(`<button class="btn ghost icon sm" aria-label="Edit">${icon('edit')}</button>`); edit.onclick = (e) => { e.stopPropagation(); Device.editDevice(d, draw); };
        const del = el(`<button class="btn ghost icon sm" aria-label="Remove">${icon('trash')}</button>`); del.onclick = (e) => { e.stopPropagation(); Device.removeDevice(d, draw); };
        item.append(flash, edit, del);
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
      body.append(UI.advanced('How often to check the lights', 'JimboLED asks each light how it is doing every few seconds, so changes made in the WLED app show up here too. The defaults suit almost everyone.',
        h('div', { class: 'form-grid' },
          UI.field('Check each light every (seconds)', poll, 'Lower is quicker to notice changes but busier for the Pi. 5 is a good value.'),
          UI.field('Wait for an answer for up to (seconds)', timeout, 'After this long a light counts as offline until it answers again.')),
        h('div', { class: 'row end' }, save)));
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
        const flash = el(`<button class="btn ghost icon sm" title="Flash it, to see which light it is" aria-label="Flash it">${icon('zap')}</button>`);
        flash.onclick = () => api.post('/api/discover/identify', { host: f.host }).then((r) => UI.toast(`${f.name || f.host}: ${r.message}`, 'info')).catch(UI.notifyError);
        item.append(flash);
        if (f.known) item.append(el(`<span class="badge ok">added</span>`));
        else { const b = el(`<button class="btn sm primary" data-busy="Adding…">${icon('plus')} Add</button>`); b.onclick = () => UI.busy(b, Device.addController({ host: f.host, name: f.name }).then((r) => { if (!r) return; UI.toast(r.followed ? r.message : `Added ${f.name || f.host}`, 'success'); f.known = true; draw(s); onDone && onDone(); if (window.App) App.refresh(); }).catch(UI.notifyError)); item.append(b); }
        list.append(item);
      }
      const fresh = all.filter((f) => !f.known && f.name);
      if (!s.running && fresh.length > 1) {
        const addAll = el(`<button class="btn primary" data-busy="Adding…">${icon('plus')} Add all ${fresh.length}</button>`);
        addAll.onclick = () => UI.busy(addAll, (async () => {
          let added = 0;
          for (const f of fresh) { try { if (await Device.addController({ host: f.host, name: f.name })) { f.known = true; added++; } } catch (e) { UI.notifyError(e); } }
          UI.toast(`Added ${added} controller${added === 1 ? '' : 's'}`, 'success'); draw(s); onDone && onDone(); if (window.App) App.refresh(true);
        })());
        list.prepend(h('div', { class: 'row end' }, addAll));
      }
      if (s.errors && s.errors.length) list.append(el(`<div class="alert warn">${esc(s.errors.join(' '))}</div>`));
    }
    start(null);
  }

  // --------------------------------------------------------- switches
  /** 60 -> "1 minute", 90 -> "90 seconds", 3600 -> "1 hour". */
  function plainTime(sec) {
    const n = (v, unit) => `${+v.toFixed(1)} ${unit}${+v.toFixed(1) === 1 ? '' : 's'}`;
    if (sec < 60 || sec % 60) return n(sec, 'second');
    if (sec < 3600 || sec % 3600) return n(sec / 60, 'minute');
    return n(sec / 3600, 'hour');
  }

  /** One plain sentence about a switch, for the list. */
  function describeSwitch(s, all, pins) {
    const pin = (pins || []).find((p) => p.bcm === s.pin);
    const bits = [GPIO.MODE_LABEL[s.mode] || s.mode, `GPIO ${s.pin}${pin && pin.physical ? ` (pin ${pin.physical})` : ''}`];
    const partners = s.interlock_group ? all.filter((o) => o.id !== s.id && o.interlock_group === s.interlock_group).map((o) => o.name) : [];
    if (partners.length) bits.push(`never together with ${partners.join(', ')}`);
    if (s.max_on_seconds) bits.push(`stops by itself after ${plainTime(s.max_on_seconds)}`);
    if (!s.enabled) bits.push('turned off in settings');
    return bits.join(' · ');
  }

  async function switchesSection(body) {
    let gpio;
    try { gpio = await api.get('/api/gpio'); } catch (e) { return UI.notifyError(e); }
    body.append(UI.intro('switch', 'Relays & switches', 'Each button here clicks one relay on your relay board, wired to the Raspberry Pi. Use it for bed motors, lamps, fans, garage doors, anything a relay can switch. Every button you add appears on the dashboard. A motor needs two steps there: tap <b>Enable</b>, then press and hold.'));
    if (gpio.simulated) body.append(el(`<div class="sim-banner">${icon('alert')}<div><b>Practice mode.</b> This computer has no relay pins (${esc(gpio.backend)}), so the buttons only pretend to work. On the Raspberry Pi they click real relays.</div></div>`));
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const fixes = h('div', { class: 'stack' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} Add a button</button>`);
    addBtn.onclick = () => GPIO.editSwitch(null, draw);
    const motorBtn = el(`<button class="btn">${icon('bed')} Add a motor (Up + Down)</button>`);
    motorBtn.onclick = () => GPIO.editSwitch(null, draw, { mode: 'momentary', pair: true });
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Dashboard buttons' }), h('div', { class: 'row' }, motorBtn, addBtn)), fixes, list);
    body.append(card);
    Automation.buttonsCard(body);
    async function draw() {
      list.innerHTML = ''; fixes.innerHTML = '';
      let cfg, pins = [], tiles = [];
      try {
        [cfg, gpio, pins, tiles] = await Promise.all([
          api.get('/api/gpio/switches').then((r) => r.switches), api.get('/api/gpio'),
          api.get('/api/gpio/pins').then((r) => r.pins).catch(() => []),
          api.get('/api/dashboard').then((r) => r.dashboard.tiles || []).catch(() => [])]);
      } catch (e) { return UI.notifyError(e); }
      if (!cfg.length) list.append(el(`<div class="empty">${icon('switch')}<h3>No relay buttons yet</h3><p>Got an adjustable bed or another motor? Press <b>Add a motor (Up + Down)</b>. For a lamp, fan or anything else, press <b>Add a button</b>.</p></div>`));
      const live = Object.fromEntries((gpio.switches || []).map((s) => [s.id, s]));
      // A hold-to-run switch on its own is almost always half a motor: offer its other half.
      for (const s of cfg) {
        if (s.mode !== 'momentary') continue;
        if (s.interlock_group && cfg.some((o) => o.id !== s.id && o.interlock_group === s.interlock_group)) continue;
        const preset = GPIO.partnerPreset(s);
        const b = el(`<button class="btn sm primary">${icon('plus')} Add ${esc(preset.name)}</button>`);
        b.onclick = () => GPIO.editSwitch(null, draw, preset);
        fixes.append(h('div', { class: 'alert info row between wrap' }, h('span', {}, h('b', { text: s.name }), ' has no opposite button yet. A motor usually needs both directions.'), b));
      }
      for (const s of cfg) {
        const st = live[s.id] || {};
        const tile = tiles.find((t) => t.type === 'switch' && t.ref === s.id);
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(s.color || 'var(--accent)')}">${icon(GPIO.switchIcon(s))}</span>`));
        const problem = st.error && st.error !== 'disabled' ? st.error : '';
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name }), h('span', { class: 'sub', text: describeSwitch(s, cfg, pins) }), problem ? h('span', { class: 'sub bad', text: problem }) : null));
        if (tile && tile.hidden) {
          const show = el(`<button class="btn sm" title="It is hidden from the dashboard">${icon('eye')} Show on dashboard</button>`);
          show.onclick = () => api.put(`/api/dashboard/tiles/${tile.id}`, { hidden: false }).then(() => { UI.toast(`${s.name} is back on the dashboard`, 'success'); draw(); if (window.App) App.refresh(true); }).catch(UI.notifyError);
          item.append(show);
        }
        item.append(el(`<span class="status-dot ${st.on ? 'ok' : (st.available ? '' : 'warn')}" title="${st.on ? 'running' : 'off'}"></span>`));
        const copy = el(`<button class="btn ghost icon sm" aria-label="Copy ${esc(s.name)}" title="Make a copy (for another relay)">${icon('stack')}</button>`); copy.onclick = () => GPIO.editSwitch(null, draw, GPIO.copyPreset(s));
        const edit = el(`<button class="btn ghost icon sm" aria-label="Edit ${esc(s.name)}" title="Edit">${icon('edit')}</button>`); edit.onclick = () => GPIO.editSwitch(s, draw);
        const del = el(`<button class="btn ghost icon sm" aria-label="Remove ${esc(s.name)}" title="Remove">${icon('trash')}</button>`); del.onclick = () => GPIO.removeSwitch(s, draw);
        item.append(copy, edit, del);
        list.append(item);
      }
    }
    draw();
    // global gpio settings
    const settings = await api.get('/api/settings').catch(() => null);
    if (settings) {
      const dead = h('input', { class: 'input', type: 'number', min: 0, max: 5000, value: settings.gpio.interlock_dead_time_ms });
      const hold = h('input', { class: 'input', type: 'number', min: 0.5, max: 10, step: 0.1, value: settings.gpio.hold_timeout_s });
      const enableFor = h('input', { class: 'input', type: 'number', min: 3, max: 120, step: 1, value: settings.gpio.enable_window_s == null ? 15 : settings.gpio.enable_window_s });
      const backend = h('select', { class: 'select' });
      [['auto', 'Automatic (recommended)'], ['lgpio', 'lgpio (Raspberry Pi OS Bookworm and newer)'], ['rpigpio', 'RPi.GPIO (older Raspberry Pi OS)'], ['native', 'Native'], ['mock', 'Practice mode (no relays)']].forEach(([v, t]) => backend.append(h('option', { value: v, text: t, selected: settings.gpio.backend === v })));
      const save = el(`<button class="btn" data-busy="Saving…">Save</button>`);
      save.onclick = () => UI.busy(save, api.put('/api/settings', { gpio: { interlock_dead_time_ms: +dead.value, hold_timeout_s: +hold.value, enable_window_s: +enableFor.value, backend: backend.value } }).then(() => { UI.toast('Saved', 'success'); draw(); }).catch(UI.notifyError));
      body.append(UI.advanced('Safety timing and hardware', 'The defaults suit almost everyone. Change these only if a motor behaves oddly.',
        h('div', { class: 'form-grid' },
          UI.field('Pause when changing direction (milliseconds)', dead, 'When a motor goes from Up straight to Down, wait this long in between so it never gets both at once. 300 is a good value.'),
          UI.field('Motors stay enabled for (seconds)', enableFor, 'After Enable is tapped, or after the motor last moved. Then it locks itself again. 15 is a good value.'),
          UI.field('Stop if the phone loses contact for (seconds)', hold, 'While you hold a button, your phone keeps telling the Pi. If that stops (Wi‑Fi drops, phone locks), the motor stops after this long.'),
          UI.field('Relay driver', backend, `Leave on Automatic. Now using ${gpio.backend}${gpio.pi_model ? ' on a ' + gpio.pi_model : ''}.`)),
        h('div', { class: 'row end' }, save)));
      const evBtn = el(`<button class="btn">${icon('activity')} What happened recently?</button>`);
      evBtn.onclick = async () => { const r = await api.get('/api/gpio/events'); const log = h('div', { class: 'log' }); for (const e of r.events.slice().reverse()) log.append(h('div', { text: `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.name}: ${e.action}${e.reason ? ' (' + e.reason + ')' : ''} via ${e.source}` })); if (!r.events.length) log.textContent = 'Nothing yet.'; UI.modal({ title: 'Recent relay activity', icon: 'activity', body: log, wide: true }); };
      body.append(h('div', { class: 'row' }, evBtn));
    }
  }
  /** Kept for the setup wizard: the motor pair form, pre-set for a bed. */
  function bedTemplate(onDone) {
    return GPIO.editSwitch(null, () => { onDone && onDone(); }, { mode: 'momentary', pair: true, base: 'Bed' });
  }

  // ----------------------------------------------------------- scenes
  async function scenesSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} New scene</button>`);
    addBtn.onclick = () => sceneDialog(null, draw);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Your scenes' }), addBtn), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      const scenes = (await api.get('/api/scenes').catch(() => ({ scenes: [] }))).scenes;
      if (!scenes.length) list.append(el(`<div class="empty">${icon('sparkles')}<h3>No scenes yet</h3><p>Press <b>New scene</b> to make your first one.</p></div>`));
      for (const s of scenes) {
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(s.color || 'var(--accent)')}">${icon(s.icon || 'sparkles')}</span>`), h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name }), h('span', { class: 'sub', text: sceneSummary(s) })));
        const run = el(`<button class="btn sm">${icon('play')} Run</button>`); run.onclick = () => api.post(`/api/scenes/${s.id}/run`, {}, { timeout: 75000 }).then((r) => UI.toast(r.failed.length ? 'Scene ran with errors: ' + r.failed.map((f) => f.error).join('; ') : 'Scene applied', r.failed.length ? 'error' : 'success')).catch(UI.notifyError);
        const edit = el(`<button class="btn ghost icon sm">${icon('edit')}</button>`); edit.onclick = () => sceneDialog(s, draw);
        const del = el(`<button class="btn ghost icon sm">${icon('trash')}</button>`); del.onclick = async () => { if (await UI.confirm({ title: 'Delete scene', message: `Delete "${s.name}"?`, okText: 'Delete', danger: true })) { await api.del(`/api/scenes/${s.id}`).catch(UI.notifyError); draw(); if (window.App) App.refresh(); } };
        item.append(run, edit, del);
        list.append(item);
      }
    }
    draw();
  }
  function sceneSummary(s) {
    const bits = s.actions.slice(0, 3).map((a) => {
      if (a.type === 'lights') return `${Lights.targetLabel(a.target)}: ${Lights.describeIntent(a.intent)}`;
      if (a.type === 'device') return a.label || 'one light';
      if (a.type === 'all') return 'all lights';
      if (a.type === 'switch') return `switch ${a.action}`;
      if (a.type === 'all_off') return 'everything off';
      return `wait ${((a.ms || 0) / 1000).toFixed(1)} s`;
    });
    return bits.join(' → ') + (s.actions.length > 3 ? ` (+${s.actions.length - 3} more)` : '');
  }
  async function sceneDialog(existing, onDone) {
    const [devs, sws] = await Promise.all([api.get('/api/devices').then((r) => r.devices), api.get('/api/gpio/switches').then((r) => r.switches)]);
    const scene = existing ? JSON.parse(JSON.stringify(existing)) : { name: '', icon: 'sparkles', color: '', actions: [] };
    const name = h('input', { class: 'input', value: scene.name, placeholder: 'e.g. Goodnight', maxlength: 60 });
    const list = h('div', { class: 'list' });
    const editors = new Map(); // action -> {target, intent} controls of a "lights" row
    const TYPES = [['lights', 'Lights: colour, preset, effect…'], ['device', 'One light: exact saved look'], ['all', 'All lights: exact state'], ['switch', 'A relay button'], ['all_off', 'Everything off'], ['delay', 'Wait a moment']];
    /** Pull what is typed into "lights" rows back into the actions (before a redraw or save). */
    function collect(strict) {
      for (const [a, ed] of editors) {
        // Separately: an unchosen target must not throw away the edits
        // made to the light settings beside it (and the other way round).
        try { a.target = ed.target.getValue(); } catch (e) { if (strict) throw e; }
        try { a.intent = ed.intent.getValue(); } catch (e) { if (strict) throw e; }
      }
    }
    function actionRow(a, i) {
      const row = h('div', { class: 'list-item scene-action', style: { flexWrap: 'wrap' } });
      const type = h('select', { class: 'select', style: { width: '230px' } });
      // "All lights: exact state" sends the same numbers to every controller,
      // and preset or effect numbers mean different things on each one, so
      // it is only kept for scenes that already use it.  "Lights" does it by name.
      TYPES.filter(([v]) => v !== 'all' || a.type === 'all').forEach(([v, t]) => type.append(h('option', { value: v, text: t, selected: a.type === v })));
      type.onchange = () => { collect(false); a.type = type.value; if (a.type === 'device') { a.ref = devs[0]?.id; a.state = a.state || { on: true }; } if (a.type === 'all') { a.state = a.state || { on: true }; } if (a.type === 'switch') { a.ref = sws[0]?.id; a.action = 'off'; } if (a.type === 'delay') { a.ms = 1000; } if (a.type === 'lights') { a.target = a.target || 'all'; a.intent = a.intent || { on: true }; } draw(); };
      row.append(type);
      if (a.type === 'lights') {
        const tgt = Lights.targetSelect(a.target || 'all');
        const intent = Lights.intentEditor(a.intent || { on: true });
        editors.set(a, { target: tgt, intent });
        row.append(tgt, h('div', { style: { flexBasis: '100%' } }, intent));
      } else if (a.type === 'device' || a.type === 'all') {
        if (a.type === 'device') { const ref = h('select', { class: 'select', style: { width: '180px' } }); for (const d of devs) ref.append(h('option', { value: d.id, text: d.name, selected: d.id === a.ref })); ref.onchange = () => { a.ref = ref.value; }; row.append(ref); }
        const st = a.state || {};
        const on = h('select', { class: 'select', style: { width: '110px' } }); [['on', 'Turn on'], ['off', 'Turn off']].forEach(([v, t]) => on.append(h('option', { value: v, text: t, selected: (st.on === false ? 'off' : 'on') === v }))); on.onchange = () => { a.state.on = on.value === 'on'; };
        const bri = h('input', { class: 'input', type: 'number', min: 1, max: 255, value: st.bri || '', placeholder: 'brightness', style: { width: '110px' } }); bri.onchange = () => { if (bri.value) a.state.bri = +bri.value; else delete a.state.bri; };
        row.append(on, bri);
        if (Array.isArray(st.seg)) {
          // A captured look: every segment exactly as it was.  Not editable
          // field by field, but it can be re-captured.
          row.append(h('span', { class: 'badge accent', title: 'Colours, effect and segments exactly as they were captured', text: `saved look · ${st.seg.length} segment${st.seg.length === 1 ? '' : 's'}` }));
        } else {
          const ps = h('input', { class: 'input', type: 'number', min: 1, max: 250, value: st.ps || '', placeholder: 'preset #', style: { width: '100px' } }); ps.onchange = () => { if (ps.value) a.state.ps = +ps.value; else delete a.state.ps; };
          const first = st.seg && Array.isArray(st.seg.col) ? st.seg.col[0] : null;  // [r,g,b], or a kelvin number
          const col = h('input', { class: 'input mono', value: Array.isArray(first) ? Color.rgbToHex(first) : (first ? String(first) : ''), placeholder: '#colour', style: { width: '100px' } }); col.onchange = () => { const rgb = Color.hexToRgb(col.value); if (rgb) a.state.seg = { col: [rgb], fx: 0 }; else delete a.state.seg; };
          row.append(ps, col);
        }
      } else if (a.type === 'switch') {
        const ref = h('select', { class: 'select', style: { width: '180px' } }); for (const s of sws) ref.append(h('option', { value: s.id, text: s.name, selected: s.id === a.ref })); ref.onchange = () => { a.ref = ref.value; };
        const act = h('select', { class: 'select', style: { width: '150px' } }); [['off', 'Turn off'], ['on', 'Turn on'], ['toggle', 'Switch over'], ['pulse', 'Tap (press briefly)']].forEach(([v, t]) => act.append(h('option', { value: v, text: t, selected: a.action === v }))); act.onchange = () => { a.action = act.value; };
        row.append(ref, act, h('span', { class: 'small muted', text: 'a motor (hold-to-run) can only be turned off by a scene' }));
      } else if (a.type === 'all_off') {
        row.append(h('span', { class: 'small muted', text: 'every light and every relay off' }));
      } else {
        const ms = h('input', { class: 'input', type: 'number', min: 0, max: 10000, step: 100, value: a.ms || 1000, style: { width: '120px' } }); ms.onchange = () => { a.ms = +ms.value; };
        row.append(ms, h('span', { class: 'small muted', text: 'milliseconds' }));
      }
      const up = el(`<button class="btn ghost icon sm" title="Move up">${icon('up')}</button>`); up.onclick = () => { if (i > 0) { collect(false); scene.actions.splice(i - 1, 0, scene.actions.splice(i, 1)[0]); draw(); } };
      const rm = el(`<button class="btn ghost icon sm" title="Remove">${icon('x')}</button>`); rm.onclick = () => { collect(false); scene.actions.splice(i, 1); draw(); };
      row.append(h('div', { class: 'row', style: { marginLeft: 'auto', gap: '2px' } }, up, rm));
      return row;
    }
    function draw() { editors.clear(); list.innerHTML = ''; scene.actions.forEach((a, i) => list.append(actionRow(a, i))); if (!scene.actions.length) list.append(h('div', { class: 'small muted', text: 'No actions yet. Add one, or capture how the lights look right now.' })); }
    const add = el(`<button class="btn sm">${icon('plus')} Add action</button>`); add.onclick = () => { collect(false); scene.actions.push({ type: 'lights', target: 'all', intent: { on: true } }); draw(); };
    const capture = el(`<button class="btn sm" data-busy="Capturing…">${icon('save')} Capture the lights as they are now</button>`);
    capture.onclick = () => UI.busy(capture, (async () => { collect(false); const r = await api.post('/api/lights/capture', { target: 'all' }); scene.actions.push(...r.actions); draw(); UI.toast(`Captured ${r.captured} light${r.captured === 1 ? '' : 's'}${r.captured < r.of ? ` (${r.of - r.captured} offline)` : ''}`, 'success'); })().catch(UI.notifyError));
    const body = h('div', { class: 'stack' }, UI.field('Scene name', name), h('h4', { text: 'Actions (run in order)' }), list, h('div', { class: 'row wrap' }, add, capture), h('h4', { text: 'Icon' }), UI.iconPicker(scene.icon, (n) => { scene.icon = n; }), h('h4', { text: 'Colour' }), UI.colorPicker(scene.color, (c) => { scene.color = c; }));
    draw();
    const save = el(`<button class="btn primary" data-busy="Saving…">Save scene</button>`);
    const tryIt = el(`<button class="btn" data-busy="Running…">${icon('play')} Try it</button>`);
    tryIt.onclick = () => UI.busy(tryIt, (async () => {
      collect(true);
      const r = await api.post('/api/scenes/test', { actions: scene.actions }, { timeout: 75000 });
      UI.toast(r.failed.length ? 'Ran with problems: ' + r.failed.map((f) => f.error).join('; ') : 'That is how it looks', r.failed.length ? 'error' : 'success');
      if (window.App) App.refresh();
    })().catch(UI.notifyError));
    const m = UI.modal({ title: existing ? 'Edit scene' : 'New scene', icon: 'sparkles', body, footer: [tryIt, save], wide: true, sticky: true });
    save.onclick = () => UI.busy(save, (async () => {
      collect(true);
      scene.name = name.value.trim();
      const payload = { ...scene, actions: scene.actions };
      if (existing) await api.put(`/api/scenes/${existing.id}`, payload); else await api.post('/api/scenes', payload);
      UI.toast('Scene saved', 'success'); m.close(); onDone && onDone(); if (window.App) App.refresh(true);
    })().catch(UI.notifyError));
  }


  // ---------------------------------------------------- emergency stop
  const SCOPE_LABEL = { all: 'Every relay', group: 'A motor (its Up and Down together)', switch: 'Buttons I pick' };

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
        ? [...new Set(switches.map((s) => s.interlock_group).filter(Boolean))].map((g) => [g, switches.filter((s) => s.interlock_group === g).map((s) => s.name).join(' + ')])
        : switches.map((s) => [s.id, s.name]);
      if (!options.length) {
        refsWrap.append(el(`<div class="alert warn">${scope.value === 'group'
          ? 'No motors yet. Add one under Settings, Relays & switches, with <b>Add a motor (Up + Down)</b>.'
          : 'No relay buttons yet. Add one under Settings, Relays & switches.'}</div>`));
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
      UI.field('What does it stop?', scope),
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
        h('div', { class: 'full' }, UI.field('Where does the other wire go?', pull, 'GND (ground) is the usual choice.'))),
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
    const prefsCard = h('div', { class: 'card' });
    body.append(
      zonesCard, inputsCard, prefsCard,
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
      const addInput = el(`<button class="btn">${icon('plus')} Add a stop button</button>`);
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

      // ---- preferences
      prefsCard.innerHTML = '';
      const masterName = h('input', { class: 'input', value: estop.master_name || 'All relays', maxlength: 60 });
      const confirmChk = h('input', { type: 'checkbox', checked: !!estop.confirm_engage });
      const savePrefs = el(`<button class="btn primary" data-busy="Saving…">Save</button>`);
      savePrefs.onclick = () => UI.busy(savePrefs, api.put('/api/estop/settings', {
        master_name: masterName.value.trim() || 'All relays', confirm_engage: confirmChk.checked,
      }).then(() => { UI.toast('Saved', 'success'); if (window.App) App.refresh(true); draw(); }).catch(UI.notifyError));
      prefsCard.append(
        h('div', { class: 'card-title' }, h('h3', { text: 'Options' })),
        UI.field('Name of the master stop', masterName, 'What the built-in stop is called on the button and in the log.'),
        h('label', { class: 'check' }, confirmChk, h('span', { text: 'Ask "are you sure?" before engaging a stop' })),
        el(`<p class="muted small">Off by default. In a real emergency a dialog is one more thing between you and the motor — turn this on only if you keep catching the button by accident.</p>`),
        h('div', { class: 'row end' }, savePrefs));
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

  const FONTS = [['system', 'System'], ['rounded', 'Rounded'], ['humanist', 'Humanist'], ['condensed', 'Condensed'], ['serif', 'Serif'], ['mono', 'Mono']];
  // Pictures bigger than this are shrunk in the browser before uploading:
  // phone photos are often 5–10 MB, and a Pi Zero has better things to do.
  const PICTURE_LIMITS = { logo: { px: 512, bytes: 1024 * 1024 }, background: { px: 1920, bytes: 1536 * 1024 } };

  /** Shrink a raster picture to fit the limits; SVG and GIF go up untouched. */
  async function preparePicture(file, kind) {
    const lim = PICTURE_LIMITS[kind];
    if (/svg|gif/i.test(file.type) || /\.(svg|gif)$/i.test(file.name)) return file;
    let img;
    const url = URL.createObjectURL(file);
    try {
      img = await new Promise((resolve, reject) => { const i = new Image(); i.onload = () => resolve(i); i.onerror = () => reject(new Error('That file is not a picture this browser can open')); i.src = url; });
    } finally { URL.revokeObjectURL(url); }
    const scale = Math.min(1, lim.px / Math.max(img.naturalWidth, img.naturalHeight));
    if (scale === 1 && file.size <= lim.bytes) return file;
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    // Logos keep their transparency; a background photo compresses far better as JPEG.
    const type = kind === 'logo' ? 'image/png' : 'image/jpeg';
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, 0.85));
    if (!blob) return file;
    return new File([blob], `${kind}.${type === 'image/png' ? 'png' : 'jpg'}`, { type });
  }

  /** Preview + Upload/Remove buttons for one branding picture. */
  function picturePicker(kind, current, onChanged) {
    const preview = h('div', { class: 'picture-preview' + (kind === 'background' ? ' cover' : '') });
    const input = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/gif,image/webp,image/svg+xml,.svg', class: 'hidden', 'aria-label': `Choose a ${kind} picture` });
    const choose = el(`<button type="button" class="btn sm" data-busy="Uploading…">${icon('upload')} ${current ? 'Replace' : 'Upload'}</button>`);
    const remove = el(`<button type="button" class="btn sm ghost" data-busy="Removing…">${icon('trash')} Remove</button>`);
    const draw = (info) => {
      preview.innerHTML = '';
      preview.classList.toggle('has-image', !!info);
      if (info && kind === 'logo') preview.append(h('img', { src: info.url, alt: 'Current logo' }));
      else if (info) preview.style.backgroundImage = `url("${info.url}")`;
      else { preview.style.backgroundImage = ''; preview.append(h('span', { text: kind === 'logo' ? 'No logo' : 'No picture' })); }
      remove.hidden = !info;
      choose.lastChild.textContent = info ? ' Replace' : ' Upload';
    };
    draw(current);
    choose.onclick = () => input.click();
    input.onchange = () => {
      const file = input.files[0]; input.value = '';
      if (!file) return;
      UI.busy(choose, (async () => {
        const ready = await preparePicture(file, kind);
        const fd = new FormData(); fd.append('file', ready, ready.name || file.name);
        const r = await api.upload(`/api/branding/${kind}`, fd, { timeout: 60000 });
        draw(r.branding[kind]); onChanged(r);
        UI.toast(kind === 'logo' ? 'Logo updated' : 'Background updated', 'success');
      })().catch(UI.notifyError));
    };
    remove.onclick = async () => {
      if (!await UI.confirm({ title: kind === 'logo' ? 'Remove the logo' : 'Remove the background picture', message: 'You can upload a new one any time.', okText: 'Remove', danger: true })) return;
      UI.busy(remove, api.del(`/api/branding/${kind}`).then((r) => { draw(null); onChanged(r); UI.toast('Removed', 'success'); }).catch(UI.notifyError));
    };
    return h('div', { class: 'picture-row' }, preview, h('div', { class: 'row wrap' }, choose, remove), input);
  }

  // One-tap looks: a whole style at once.  Only look-and-feel keys, so your
  // title, logo and pictures are never touched; previewed until you save.
  const LOOKS = [
    { id: 'midnight', name: 'Midnight', theme: 'midnight', accent: '#7c5cff', font: 'system', backdrop: 'glow', surface: 'solid', radius: 'soft', density: 'comfortable' },
    { id: 'neon', name: 'Neon night', theme: 'oled', accent: '#22d3ee', font: 'condensed', backdrop: 'glow', surface: 'glass', radius: 'sharp', density: 'comfortable' },
    { id: 'cabin', name: 'Warm cabin', theme: 'graphite', accent: '#f97316', font: 'serif', backdrop: 'glow', surface: 'solid', radius: 'round', density: 'roomy' },
    { id: 'ocean', name: 'Ocean calm', theme: 'ocean', accent: '#34d399', font: 'rounded', backdrop: 'glow', surface: 'glass', radius: 'round', density: 'comfortable' },
    { id: 'studio', name: 'Studio', theme: 'graphite', accent: '#e2e8f0', font: 'humanist', backdrop: 'plain', surface: 'solid', radius: 'sharp', density: 'compact' },
    { id: 'daylight', name: 'Daylight', theme: 'daylight', accent: '#2563eb', font: 'system', backdrop: 'plain', surface: 'solid', radius: 'soft', density: 'comfortable' },
    { id: 'paper', name: 'Paper & ink', theme: 'paper', accent: '#b45309', font: 'serif', backdrop: 'plain', surface: 'solid', radius: 'sharp', density: 'roomy' },
    { id: 'photo', name: 'Photo frame', needs: 'background', theme: 'midnight', font: 'rounded', backdrop: 'image', surface: 'glass', radius: 'round', density: 'comfortable', backdrop_dim: 35 },
  ];

  /** The most common vivid colours in a picture (e.g. the logo), most used first. */
  async function pictureColors(url, max) {
    const img = await new Promise((resolve, reject) => { const i = new Image(); i.onload = () => resolve(i); i.onerror = reject; i.src = url; });
    const size = 48;
    const c = document.createElement('canvas'); c.width = size; c.height = size;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, size, size);
    const px = ctx.getImageData(0, 0, size, size).data;
    const buckets = new Map();
    for (let i = 0; i < px.length; i += 4) {
      const [r, g, b, a] = [px[i], px[i + 1], px[i + 2], px[i + 3]];
      if (a < 160) continue;
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      // Skip greys, near-black and near-white: they make poor accents.
      if (mx - mn < 40 || mx < 50 || mn > 225) continue;
      const key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
      const e = buckets.get(key) || { n: 0, r: 0, g: 0, b: 0 };
      e.n++; e.r += r; e.g += g; e.b += b; buckets.set(key, e);
    }
    const ranked = [...buckets.values()].sort((x, y) => y.n - x.n).map((e) => [e.r / e.n, e.g / e.n, e.b / e.n]);
    const out = [];
    for (const rgb of ranked) {
      if (out.every((o) => Math.hypot(o[0] - rgb[0], o[1] - rgb[1], o[2] - rgb[2]) > 70)) out.push(rgb);
      if (out.length >= (max || 5)) break;
    }
    return out.map((rgb) => '#' + rgb.map((v) => Math.round(v).toString(16).padStart(2, '0')).join(''));
  }

  async function appearanceSection(body) {
    const res = await api.get('/api/dashboard');
    const dash = res.dashboard;
    let branding = res.branding || {};
    const root = document.documentElement;
    const live = { theme: dash.theme, accent: dash.accent, density: dash.density,
                   radius: dash.radius || 'soft', text_scale: dash.text_scale || 100,
                   font: dash.font || 'system', brand_mode: dash.brand_mode || 'logo_title', logo_size: dash.logo_size || 'm',
                   backdrop: dash.backdrop || 'glow', backdrop_dim: dash.backdrop_dim == null ? 45 : dash.backdrop_dim,
                   surface: dash.surface || 'solid', custom_css: dash.custom_css || '' };
    // Everything previews on the real page as you pick it, and is put back if
    // you navigate away without saving.
    const original = { ...live };
    const apply = () => {
      root.dataset.theme = live.theme;
      root.dataset.density = live.density;
      root.dataset.radius = live.radius;
      root.dataset.font = live.font;
      root.dataset.brand = live.brand_mode;
      root.dataset.logoSize = live.logo_size;
      root.dataset.surface = live.surface;
      root.dataset.backdrop = live.backdrop === 'image' && !branding.background ? 'glow' : live.backdrop;
      root.style.setProperty('--accent', live.accent);
      root.style.setProperty('--text-scale', (live.text_scale / 100).toFixed(3));
      root.style.setProperty('--backdrop-dim', (live.backdrop_dim / 100).toFixed(2));
      if (window.App) App.setCustomCss(live.custom_css);
    };
    const pictureChanged = (r) => {
      branding = r.branding || {};
      if (window.App) { App.state.branding = branding; App.applyBranding(r.dashboard || dash, branding, { keepLook: true }); }
      if (r.dashboard && r.dashboard.backdrop && r.dashboard.backdrop !== live.backdrop) {
        live.backdrop = original.backdrop = r.dashboard.backdrop;
        backdrop.querySelectorAll('.chip').forEach((c, i) => c.classList.toggle('active', BACKDROPS[i][0] === live.backdrop));
      }
      apply();
      drawLogoColors();
    };

    const title = h('input', { class: 'input', value: dash.title, maxlength: 40 });
    const subtitle = h('input', { class: 'input', value: dash.subtitle || '', maxlength: 80, placeholder: 'e.g. Jim\'s room' });
    const footer = h('input', { class: 'input', value: dash.footer_text || '', maxlength: 120, placeholder: 'e.g. Questions? Ask Jim · ext. 204' });
    title.oninput = () => { document.getElementById('brand-title').textContent = title.value || 'JimboLED'; };
    subtitle.oninput = () => { document.getElementById('brand-sub').textContent = subtitle.value; };

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
    // Colours taken from the uploaded logo, so the dashboard matches it in one tap.
    const logoColors = h('div', { class: 'row wrap logo-colors' });
    const markAccent = () => [accentRow, logoColors].forEach((row) => row.querySelectorAll('.swatch').forEach((x) => x.classList.toggle('active', x.dataset.color === live.accent)));
    for (const c of ACCENTS) {
      const b = h('button', { type: 'button', class: 'swatch lg', style: { background: c }, dataset: { color: c },
                              title: c, 'aria-label': `Accent colour ${c}` });
      b.onclick = () => { live.accent = c; custom.value = c; wheel.value = c; custom.classList.remove('invalid'); apply(); markAccent(); };
      accentRow.append(b);
    }
    const wheel = h('input', { type: 'color', class: 'color-input', value: live.accent, 'aria-label': 'Pick any accent colour' });
    wheel.oninput = () => { live.accent = wheel.value.toLowerCase(); custom.value = live.accent; custom.classList.remove('invalid'); apply(); markAccent(); };
    const custom = h('input', { class: 'input mono', value: live.accent, maxlength: 7, style: { width: '110px' },
                                'aria-label': 'Custom accent colour, hex' });
    custom.onchange = () => {
      let v = custom.value.trim().toLowerCase();
      if (/^[0-9a-f]{6}$/.test(v)) v = '#' + v;
      if (/^#[0-9a-f]{6}$/.test(v)) { custom.value = v; wheel.value = v; custom.classList.remove('invalid'); live.accent = v; apply(); markAccent(); }
      else { custom.classList.add('invalid'); UI.toast('Use a 6-digit hex colour like #7c5cff', 'error'); }
    };
    accentRow.append(wheel, custom);
    const setAccent = (c) => { live.accent = c; custom.value = c; wheel.value = c; custom.classList.remove('invalid'); apply(); markAccent(); };

    async function drawLogoColors() {
      logoColors.innerHTML = '';
      if (!branding.logo) return;
      let colors = [];
      try { colors = await pictureColors(branding.logo.url, 5); } catch (e) { return; }
      if (!colors.length) return;
      logoColors.append(h('span', { class: 'hint', text: 'From your logo:' }));
      for (const c of colors) {
        const b = h('button', { type: 'button', class: 'swatch lg', style: { background: c }, dataset: { color: c }, title: c, 'aria-label': `Use ${c} from the logo` });
        b.onclick = () => setAccent(c);
        logoColors.append(b);
      }
      markAccent();
    }
    drawLogoColors();
    markAccent();

    const font = chipGroup(FONTS, live.font, (v) => { live.font = v; apply(); });
    font.querySelectorAll('.chip').forEach((c, i) => { c.style.fontFamily = 'var(--font)'; c.dataset.font = FONTS[i][0]; });
    const DENSITY_OPTS = [['comfortable', 'Comfortable'], ['compact', 'Compact'], ['roomy', 'Roomy']];
    const RADIUS_OPTS = [['sharp', 'Sharp'], ['soft', 'Soft'], ['round', 'Round']];
    const SURFACE_OPTS = [['solid', 'Solid'], ['glass', 'Glass (see-through)']];
    const density = chipGroup(DENSITY_OPTS,
      live.density, (v) => { live.density = v; apply(); });
    const radius = chipGroup(RADIUS_OPTS,
      live.radius, (v) => { live.radius = v; apply(); });
    const brandMode = chipGroup([['logo_title', 'Logo and title'], ['logo', 'Logo only'], ['title', 'Title only']],
      live.brand_mode, (v) => { live.brand_mode = v; apply(); });
    const logoSize = chipGroup([['s', 'Small'], ['m', 'Medium'], ['l', 'Large']],
      live.logo_size, (v) => { live.logo_size = v; apply(); });
    const BACKDROPS = [['plain', 'Plain'], ['glow', 'Accent glow'], ['image', 'Picture']];
    const backdrop = chipGroup(BACKDROPS, live.backdrop, (v) => {
      live.backdrop = v; apply();
      if (v === 'image' && !branding.background) UI.toast('Upload a background picture below to see it', 'info');
    });
    const surface = chipGroup(SURFACE_OPTS,
      live.surface, (v) => { live.surface = v; apply(); });

    const markChips = (wrap, options, value) => wrap.querySelectorAll('.chip').forEach((c, i) => c.classList.toggle('active', options[i][0] === value));
    const looks = h('div', { class: 'looks' });
    for (const look of LOOKS) {
      const b = h('button', { type: 'button', class: 'look theme-swatch', dataset: { theme: look.theme, font: look.font },
                              style: { '--look-accent': look.accent || 'var(--accent)' }, title: look.name },
        h('span', { class: 'look-dots' }, h('span'), h('span'), h('span')),
        h('span', { class: 'look-name', text: look.name }));
      b.onclick = () => {
        if (look.needs === 'background' && !branding.background) { UI.toast('Upload a background picture first (below), then try Photo frame', 'info'); return; }
        for (const k of ['theme', 'font', 'backdrop', 'surface', 'radius', 'density', 'backdrop_dim']) if (look[k] != null) live[k] = look[k];
        if (look.accent) setAccent(look.accent);
        themeWrap.querySelectorAll('.chip').forEach((x) => x.classList.toggle('active', x.dataset.theme === live.theme));
        markChips(font, FONTS, live.font); markChips(backdrop, BACKDROPS, live.backdrop); markChips(surface, SURFACE_OPTS, live.surface);
        markChips(radius, RADIUS_OPTS, live.radius); markChips(density, DENSITY_OPTS, live.density);
        dim.setValue(live.backdrop_dim);
        looks.querySelectorAll('.look').forEach((x) => x.classList.toggle('active', x === b));
        apply();
        UI.toast(`${look.name} — previewing. Save to keep it.`, 'info', 2500);
      };
      looks.append(b);
    }

    const scale = UI.slider({
      icon: 'textSize', min: 85, max: 150, step: 5, value: live.text_scale,
      format: (v) => `${v}%`,
      onInput: (v) => { live.text_scale = +v; apply(); },
      onChange: (v) => { live.text_scale = +v; apply(); },
    });
    const dim = UI.slider({
      icon: 'contrast', min: 0, max: 90, step: 5, value: live.backdrop_dim,
      format: (v) => `${v}%`,
      onInput: (v) => { live.backdrop_dim = +v; apply(); },
      onChange: (v) => { live.backdrop_dim = +v; apply(); },
    });

    const css = h('textarea', { class: 'input code', spellcheck: 'false', maxlength: 20000, rows: 8,
                                placeholder: '/* e.g. make every tile title bigger */\n.tile .title { font-size: 1.2rem; }' });
    css.value = live.custom_css;
    css.oninput = UI.debounce(() => { live.custom_css = css.value; apply(); }, 250);

    const showOffline = h('input', { type: 'checkbox', checked: dash.show_offline !== false });
    const showClock = h('input', { type: 'checkbox', checked: dash.show_clock !== false });
    const showEstop = h('input', { type: 'checkbox', checked: dash.show_estop !== false });
    const showLights = h('input', { type: 'checkbox', checked: dash.show_lights_button !== false });

    const save = el(`<button class="btn primary" data-busy="Saving…">${icon('save')} Save appearance</button>`);
    save.onclick = () => { live.custom_css = css.value; UI.busy(save, api.put('/api/dashboard', {
      title: title.value.trim(), subtitle: subtitle.value.trim(), footer_text: footer.value.trim(),
      theme: live.theme, accent: live.accent, density: live.density,
      radius: live.radius, text_scale: live.text_scale, font: live.font,
      brand_mode: live.brand_mode, logo_size: live.logo_size, backdrop: live.backdrop,
      backdrop_dim: live.backdrop_dim, surface: live.surface, custom_css: live.custom_css,
      show_offline: showOffline.checked, show_clock: showClock.checked, show_estop: showEstop.checked,
      show_lights_button: showLights.checked,
    }).then((r) => {
      Object.assign(original, live); UI.toast('Saved', 'success');
      if (window.App) { App.state.dashboard = r.dashboard; App.applyTheme(r.dashboard); App.refresh(true); }
    }).catch(UI.notifyError)); };

    const revert = el(`<button class="btn ghost">Undo changes</button>`);
    revert.onclick = () => { Object.assign(live, original); apply(); if (window.App && App.state.dashboard) App.applyTheme(App.state.dashboard); render(document.getElementById('main'), 'appearance'); };

    const reset = el(`<button class="btn ghost" data-busy="Resetting…">${icon('rotate')} Reset to defaults</button>`);
    reset.onclick = async () => {
      const ok = await UI.confirm({ title: 'Reset the look', message: 'Theme, colours, fonts, background style, logo layout and custom CSS go back to how JimboLED ships. Your title, pictures, tiles, scenes and favourite colours stay.', okText: 'Reset', danger: true });
      if (!ok) return;
      UI.busy(reset, api.post('/api/dashboard/appearance/reset', {}).then((r) => {
        UI.toast('Appearance reset', 'success');
        if (window.App) { App.state.dashboard = r.dashboard; App.state.branding = r.branding; App.applyTheme(r.dashboard); App.refresh(true); }
        render(document.getElementById('main'), 'appearance');
      }).catch(UI.notifyError));
    };

    const saveBar = h('div', { class: 'row end' }, revert, save);

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Branding' }),
        h('span', { class: 'hint', text: 'Your name and logo on every screen' })),
      h('div', { class: 'form-grid' }, UI.field('Dashboard title', title), UI.field('Subtitle', subtitle)),
      UI.field('Logo', picturePicker('logo', branding.logo, pictureChanged),
        'PNG, JPEG, WebP, GIF or SVG. A square or wide logo with a transparent background looks best. It is also used as the browser tab icon and the home-screen icon.'),
      UI.field('The header shows', brandMode),
      UI.field('Logo size', logoSize),
      UI.field('Footer text', footer, 'A line at the bottom of every page: who to call, a room number, a motto.')));

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Look & feel' }),
        h('span', { class: 'hint', text: 'Previewed live — nothing is kept until you save' })),
      UI.field('Quick looks', looks, 'A whole style in one tap: theme, accent, font, background and corners. Fine-tune anything below.'),
      UI.field('Theme', themeWrap, 'Auto follows your phone or computer between light and dark.'),
      UI.field('Accent colour', h('div', { class: 'stack accent-pick' }, accentRow, logoColors)),
      UI.field('Font', font, 'Uses the fonts already on each phone or computer, so it works without the internet.'),
      UI.field('Spacing', density, 'How much room each tile gets.'),
      UI.field('Corners', radius),
      UI.field('Text size', scale, 'Larger text for reading the dashboard at arm\'s length.')));

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Background' })),
      UI.field('Background', backdrop),
      UI.field('Background picture', picturePicker('background', branding.background, pictureChanged),
        'Big photos are shrunk automatically before uploading.'),
      UI.field('Dim the picture', dim, 'Darkens (or, on a light theme, lightens) the picture so text stays easy to read.'),
      UI.field('Tiles', surface, 'Glass lets the background show through the tiles and cards.')));

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Header' })),
      h('label', { class: 'check' }, showOffline, h('span', { text: 'Show controllers that are offline' })),
      h('label', { class: 'check' }, showClock, h('span', { text: 'Show the clock in the header' })),
      h('label', { class: 'check' }, showEstop, h('span', { text: 'Show the emergency stop button in the header' })),
      h('label', { class: 'check' }, showLights, h('span', { text: 'Show the Lights button (every light at once) in the header' }))));

    body.append(h('details', { class: 'card' },
      h('summary', { class: 'card-title' }, h('h3', { text: 'Custom CSS (advanced)' })),
      el(`<p class="muted small">Extra styles added after JimboLED's own, previewed as you type. Handy variables: <code>--accent</code>, <code>--bg</code>, <code>--surface</code>, <code>--text</code>, <code>--radius</code>, <code>--font</code>. If something goes wrong, clear this box or use <b>Reset to defaults</b>.</p>`),
      css));

    const kioskBtn = el(`<button class="btn">${icon('tv')} Use this screen as a wall tablet</button>`);
    kioskBtn.onclick = () => { if (window.App) App.setKiosk(true); location.hash = '#/'; };
    const kioskUrl = `${location.origin}/#/?kiosk=1`;
    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Wall tablet mode' })),
      el(`<p class="muted small">For a tablet mounted on the wall or a screen by the bed: a clean full-screen dashboard with a big clock and no Edit or Settings buttons, so nobody changes the setup by accident. The lights, switches, scenes, <b>All off</b> and the emergency stop all keep working. The screen stays awake.</p>`),
      el(`<p class="muted small">To leave it, <b>press and hold the logo for three seconds</b>. To set up another tablet, open <code>${esc(kioskUrl)}</code> on it.</p>`),
      h('div', { class: 'row' }, kioskBtn)));

    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('h3', { text: 'Start over' })),
      el(`<p class="muted small">Puts the theme, colours, font, background style, logo layout and custom CSS back to how JimboLED ships. Your title, pictures, tiles, scenes and favourite colours stay.</p>`),
      h('div', { class: 'row' }, reset)));
    body.append(h('div', { class: 'card sticky-save' }, saveBar));

    // Favourite colours: shown first in the Lights panel and on group tiles.
    const favs = [...(dash.favorite_colors || [])];
    const favRow = h('div', { class: 'row wrap' });
    const saveFavs = () => api.put('/api/dashboard', { favorite_colors: favs }).then(() => { drawFavs(); if (window.App) App.refresh(true); }).catch(UI.notifyError);
    const pick = h('input', { type: 'color', class: 'color-input', value: '#ff8000', 'aria-label': 'Colour to add' });
    const addFav = el(`<button class="btn sm">${icon('plus')} Add</button>`);
    addFav.onclick = () => { const c = pick.value.toLowerCase(); if (!favs.includes(c)) { favs.push(c); saveFavs(); } };
    function drawFavs() {
      favRow.innerHTML = '';
      if (!favs.length) favRow.append(h('span', { class: 'small muted', text: 'None yet. Add some here, or use “Add to favourites” in the Lights panel colour picker.' }));
      favs.forEach((c, i) => {
        const b = h('button', { type: 'button', class: 'swatch lg fav', style: { background: c }, title: `Remove ${c}`, 'aria-label': `Remove favourite ${c}` });
        b.onclick = () => { favs.splice(i, 1); saveFavs(); };
        favRow.append(b);
      });
    }
    drawFavs();
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Favourite colours' }), h('span', { class: 'hint', text: 'Tap one to remove it' })), favRow, h('div', { class: 'row' }, pick, addFav)));

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
      el(`<p class="muted small">Choose something the family will remember. The WLED lights themselves have no password, so this protects the dashboard only.</p>`),
      h('div', { class: 'form-grid' }, s.server.password_set ? UI.field('Current password', current) : null, UI.field('New password', pw, 'Leave empty to remove'), UI.field('Repeat new password', pw2)),
      h('div', { class: 'row end' }, s.server.password_set ? remove : null, save)));
    // allowed host names (DNS-rebinding guard)
    const hosts = h('input', { class: 'input mono', value: (s.server.allowed_hosts || []).join(' '), placeholder: 'e.g. jimboled.mydomain.com' });
    const saveHosts = el(`<button class="btn" data-busy="Saving…">Save names</button>`);
    saveHosts.onclick = () => UI.busy(saveHosts, api.put('/api/settings', { server: { allowed_hosts: hosts.value } }).then(() => UI.toast('Saved', 'success')).catch(UI.notifyError));
    body.append(UI.advanced('Web addresses JimboLED answers to', 'For safety JimboLED only answers when opened by its IP address, a plain name, or a name ending in .local, .lan or .home. You only need this if you gave the Pi its own domain name.', UI.field('Extra names (separate them with spaces)', hosts), h('div', { class: 'row end' }, saveHosts)));
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
    stat('Relay driver', s.gpio_backend + (s.gpio_simulated ? ' (practice mode)' : ''));
    const addr = s.addresses.map((a) => `http://${a}${s.port && s.port !== 80 ? ':' + s.port : ''}`);
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'This Pi' })), stats, el(`<p class="muted small">Open the dashboard from any device on your network at <b>http://${esc(s.hostname)}.local${s.port && s.port !== 80 ? ':' + s.port : ''}</b>${addr.length ? ' or ' + addr.map((a) => '<b>' + esc(a) + '</b>').join(', ') : ''}.</p>`)));
    const card = h('div', { class: 'card' });
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Updates & maintenance' })));
    if (!s.helper) card.append(el(`<div class="alert info">Updates, restart and reboot buttons need JimboLED to be installed with <code>install.sh</code> on the Pi. Running from a plain checkout, use the command line instead.</div>`));
    const check = el(`<button class="btn" data-busy="Checking…" ${s.helper ? '' : 'disabled'}>${icon('refresh')} Check for updates</button>`);
    const out = h('div', { class: 'small muted' });
    check.onclick = () => UI.busy(check, api.post('/api/system/update/check', {}, { timeout: 90000 }).then((r) => { if (r.update_available) { out.innerHTML = `${icon('sparkles')} A new version is available (${esc(r.remote || '')}). `; const b = el(`<button class="btn sm primary" data-busy="Updating (this takes a few minutes)…">Update now</button>`); b.onclick = () => UI.busy(b, api.post('/api/system/update', {}, { timeout: 90000 }).then((r2) => { UI.toast(r2.message, 'success', 8000); out.innerHTML = `${icon('refresh', 'spin')} Updating… this takes a few minutes on a Pi Zero. The page reloads automatically when JimboLED is back.`; const startRev = r2.rev; let tries = 0; const poll = setInterval(async () => { tries++; try { const sys = await api.get('/api/system'); if ((sys.git && sys.git.rev && sys.git.rev !== startRev) || tries > 120) { clearInterval(poll); location.reload(); } } catch (e) { /* restarting */ } }, 5000); }).catch(UI.notifyError)); out.append(b); } else out.textContent = r.message || 'You are up to date.'; }).catch(UI.notifyError));
    const restart = el(`<button class="btn" data-busy="Restarting…" ${s.helper ? '' : 'disabled'}>${icon('rotate')} Restart JimboLED</button>`);
    restart.onclick = async () => { if (await UI.confirm({ title: 'Restart JimboLED', message: 'All relays will be released. The dashboard reconnects in a few seconds.', okText: 'Restart' })) UI.busy(restart, api.post('/api/system/restart').then((r) => { UI.toast(r.message, 'success'); setTimeout(() => location.reload(), 6000); }).catch(UI.notifyError)); };
    const reboot = el(`<button class="btn danger" data-busy="Rebooting…" ${s.helper ? '' : 'disabled'}>${icon('power')} Reboot the Pi</button>`);
    reboot.onclick = async () => { if (await UI.confirm({ title: 'Reboot the Raspberry Pi', message: 'The Pi will be unavailable for about a minute.', okText: 'Reboot', danger: true })) UI.busy(reboot, api.post('/api/system/reboot').then((r) => UI.toast(r.message, 'success', 10000)).catch(UI.notifyError)); };
    card.append(h('div', { class: 'row wrap' }, check, restart, reboot), out);
    body.append(card);
    const logCard = UI.advanced('Log (for troubleshooting)', 'A record of what JimboLED has been doing. Useful if something isn\'t working and you are asking for help.');
    const log = h('div', { class: 'log' });
    const refreshLogs = async () => { const r = await api.get('/api/system/logs?limit=300'); log.innerHTML = ''; for (const l of r.logs) log.append(h('div', { class: l.level, text: `${new Date(l.ts * 1000).toLocaleTimeString()} ${l.level.padEnd(7)} ${l.msg}` })); log.scrollTop = log.scrollHeight; if (!r.logs.length) log.textContent = 'No log entries yet.'; };
    const rl = el(`<button class="btn sm">${icon('refresh')} Refresh</button>`); rl.onclick = refreshLogs;
    logCard.append(h('div', { class: 'row end' }, rl), log);
    body.append(logCard); refreshLogs();
  }

  // ----------------------------------------------------------- backup
  async function backupSection(body) {
    const dl = el(`<a class="btn primary" href="/api/backup" download>${icon('download')} Download backup</a>`);
    const file = h('input', { type: 'file', accept: 'application/json,.json', class: 'input' });
    const up = el(`<button class="btn" data-busy="Restoring…">${icon('upload')} Restore from file</button>`);
    up.onclick = () => { if (!file.files[0]) return UI.toast('Choose a backup file first', 'error'); UI.confirm({ title: 'Restore backup', message: 'This replaces all controllers, switches and dashboard settings with the ones in the file.', okText: 'Restore' }).then((ok) => { if (!ok) return; const fd = new FormData(); fd.append('file', file.files[0]); UI.busy(up, api.upload('/api/restore', fd, { timeout: 120000 }).then(() => { UI.toast('Backup restored', 'success'); if (window.App) App.refresh(true); draw(); }).catch(UI.notifyError)); }); };
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'Backup & restore' })), el(`<p class="muted small">Download a backup once everything is set up the way you like it, and keep the file somewhere safe. To put it back, choose the file and press <b>Restore from file</b>. Presets saved inside the WLED lights themselves are not included.</p>`), h('div', { class: 'row wrap' }, dl), h('div', { class: 'row wrap' }, file, up)));
    const card = h('div', { class: 'card' }); const list = h('div', { class: 'list' });
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Automatic snapshots' })), el(`<p class="muted small">Made by JimboLED itself before each important change. To undo a mistake, press <b>Restore</b> on the one from just before it.</p>`), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      const r = await api.get('/api/backups');
      if (!r.backups.length) list.append(h('div', { class: 'muted small', text: 'No snapshots yet.' }));
      for (const b of r.backups.slice(0, 15)) { const item = h('div', { class: 'list-item' }); const reason = b.name.replace(/^config-\d{8}-\d{6}-/, '').replace(/\.json$/, '').replace(/[-_]+/g, ' '); item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: new Date(b.mtime * 1000).toLocaleString() }), h('span', { class: 'sub', text: `before: ${reason}` }))); const rb = el(`<button class="btn sm" data-busy="Restoring…">Restore</button>`); rb.onclick = async () => { if (await UI.confirm({ title: 'Restore snapshot', message: `Go back to the configuration from ${new Date(b.mtime * 1000).toLocaleString()}?`, okText: 'Restore' })) UI.busy(rb, api.post(`/api/backups/${b.name}/restore`).then(() => { UI.toast('Snapshot restored', 'success'); if (window.App) App.refresh(true); }).catch(UI.notifyError)); }; item.append(rb); list.append(item); }
    }
    draw();
  }

  window.Settings = { render, discoverDialog, bedTemplate, sceneDialog, sceneSummary, pinSelect, SECTIONS };
})();
