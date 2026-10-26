/* Settings pages for groups (rooms), schedules & timers, and integrations
   (API tokens).  Rendered by Settings.render(). */
(function () {
  const { h, el, esc } = UI;
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  // ------------------------------------------------------------ groups
  async function groupsSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} New group</button>`);
    addBtn.onclick = () => groupDialog(null, draw);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Groups' }), addBtn),
      el(`<p class="muted small">Put controllers that belong together – a room, the outside lights, the bed – into a group. Each group gets its own dashboard tile and can be switched, coloured and given presets in one tap. A controller can be in several groups.</p>`), list);
    body.append(card);
    async function draw() {
      list.innerHTML = '';
      let groups = [], devs = [];
      try { [groups, devs] = await Promise.all([api.get('/api/groups').then((r) => r.groups), api.get('/api/devices').then((r) => r.devices)]); } catch (e) { return UI.notifyError(e); }
      const names = Object.fromEntries(devs.map((d) => [d.id, d.name]));
      if (!groups.length) list.append(el(`<div class="empty">${icon('home')}<h3>No groups yet</h3><p>For example “Bedroom” with the bed strip and the ceiling light.</p></div>`));
      for (const g of groups) {
        const item = h('div', { class: 'list-item' });
        item.append(el(`<span class="tile-icon" style="--tile-color:${esc(g.color || 'var(--accent)')}">${icon(g.icon || 'home')}</span>`));
        item.append(h('div', { class: 'grow' }, h('span', { class: 'title', text: g.name }), h('span', { class: 'sub', text: g.devices.length ? g.devices.map((id) => names[id] || id).join(', ') : 'No lights in this group yet' })));
        const ctl = el(`<button class="btn sm">${icon('sliders')} Control</button>`); ctl.onclick = () => Lights.open('group:' + g.id);
        const edit = el(`<button class="btn ghost icon sm" aria-label="Edit">${icon('edit')}</button>`); edit.onclick = () => groupDialog(g, draw);
        const del = el(`<button class="btn ghost icon sm" aria-label="Delete">${icon('trash')}</button>`);
        del.onclick = async () => { if (await UI.confirm({ title: 'Delete group', message: `Delete the group “${g.name}”? The lights themselves are not affected.`, okText: 'Delete', danger: true })) { await api.del(`/api/groups/${g.id}`).catch(UI.notifyError); draw(); if (window.App) App.refresh(true); } };
        item.append(ctl, edit, del);
        list.append(item);
      }
    }
    draw();
  }

  async function groupDialog(existing, onDone) {
    let devs = [];
    try { devs = (await api.get('/api/devices')).devices; } catch (e) { return UI.notifyError(e); }
    const g = existing ? JSON.parse(JSON.stringify(existing)) : { name: '', icon: 'home', color: '', devices: [] };
    const chosen = new Set(g.devices);
    const name = h('input', { class: 'input', value: g.name, placeholder: 'e.g. Bedroom', maxlength: 40 });
    const chips = h('div', { class: 'chips' });
    for (const d of devs) {
      const c = h('button', { type: 'button', class: 'chip' + (chosen.has(d.id) ? ' active' : ''), html: icon(d.icon || 'bulb') + '<span></span>' });
      c.querySelector('span').textContent = d.name;
      c.onclick = () => { chosen.has(d.id) ? chosen.delete(d.id) : chosen.add(d.id); c.classList.toggle('active', chosen.has(d.id)); };
      chips.append(c);
    }
    if (!devs.length) chips.append(h('span', { class: 'small muted', text: 'Add some controllers first.' }));
    const body = h('div', { class: 'stack' }, UI.field('Name', name), UI.field('Lights in this group', chips, 'Tap to include or leave out.'),
      h('h4', { text: 'Icon' }), UI.iconPicker(g.icon, (n) => { g.icon = n; }), h('h4', { text: 'Colour' }), UI.colorPicker(g.color, (c) => { g.color = c; }));
    const save = el(`<button class="btn primary" data-busy="Saving…">${existing ? 'Save group' : 'Create group'}</button>`);
    const m = UI.modal({ title: existing ? 'Edit group' : 'New group', icon: 'home', body, footer: [save], wide: true, sticky: true });
    save.onclick = () => UI.busy(save, (async () => {
      const payload = { name: name.value.trim(), icon: g.icon, color: g.color, devices: [...chosen] };
      if (!payload.name) throw new Error('Give the group a name');
      if (existing) await api.put(`/api/groups/${existing.id}`, payload); else await api.post('/api/groups', payload);
      UI.toast('Group saved', 'success'); m.close(); onDone && onDone(); Lights.clearCache(); if (window.App) App.refresh(true);
    })().catch(UI.notifyError));
  }

  // --------------------------------------------------------- schedules
  function daysLabel(days) {
    const d = [...(days || [])].sort();
    if (d.length === 7) return 'every day';
    if (d.join() === '0,1,2,3,4') return 'weekdays';
    if (d.join() === '5,6') return 'weekends';
    return d.map((i) => DAYS[i]).join(', ');
  }
  function whenLabel(s) {
    const w = s.when || {};
    if (w.type === 'once') return 'once, ' + new Date(w.at * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    let base;
    if (w.type === 'time') base = w.time;
    else { const off = w.offset || 0; base = off ? `${Math.abs(off)} min ${off < 0 ? 'before' : 'after'} ${w.event}` : `at ${w.event}`; }
    return `${base}, ${daysLabel(s.days)}` + (s.random ? ` (±${s.random} min)` : '');
  }
  function actionLabel(s, ctx) {
    const a = s.action || {};
    if (a.type === 'scene') { const sc = ctx.scenes.find((x) => x.id === a.ref); return `run “${sc ? sc.name : 'deleted scene'}”`; }
    if (a.type === 'switch') { const sw = ctx.switches.find((x) => x.id === a.ref); return `${a.action} ${sw ? sw.name : 'deleted switch'}`; }
    if (a.type === 'all_off') return 'everything off';
    if (a.type === 'lights') return `${Lights.targetLabel(a.target)}: ${Lights.describeIntent(a.intent)}`;
    return a.type;
  }
  const whenIcon = (s) => ({ time: 'clock', once: 'timer' }[s.when.type] || (s.when.event === 'sunrise' ? 'sun' : 'moon'));

  async function schedulesSection(body) {
    const locCard = h('div', { class: 'card' });
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} New schedule</button>`);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Schedules & timers' }), addBtn),
      el(`<p class="muted small">Run lights, scenes or switches at set times – “porch lights on 10 minutes after sunset”, “wake-up light at 6:45 on weekdays”. They run on the Pi, so nobody needs to have the dashboard open. A schedule that was due while the Pi was off is skipped rather than run late.</p>`), list);
    body.append(locCard, card);
    let ctx = { scenes: [], switches: [], listing: null };
    addBtn.onclick = () => scheduleDialog(null, ctx, draw);
    async function draw() {
      try {
        const [listing, scenes, switches] = await Promise.all([api.get('/api/schedules'), api.get('/api/scenes').then((r) => r.scenes), api.get('/api/gpio/switches').then((r) => r.switches)]);
        ctx = { scenes, switches, listing };
      } catch (e) { return UI.notifyError(e); }
      drawLocation(ctx.listing);
      list.innerHTML = '';
      const items = ctx.listing.schedules;
      if (!items.length) list.append(el(`<div class="empty">${icon('clock')}<h3>No schedules yet</h3><p>Tap <b>New schedule</b>. One-off “switch off in an hour” timers can also be set from the Lights panel.</p></div>`));
      for (const s of items) {
        const item = h('div', { class: 'list-item' + (s.enabled ? '' : ' muted-item') });
        item.append(el(`<span class="tile-icon">${icon(whenIcon(s))}</span>`));
        const subParts = [whenLabel(s), actionLabel(s, ctx)];
        const info = h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name || actionLabel(s, ctx) }), h('span', { class: 'sub', text: subParts.join(' · ') }));
        const meta = h('div', { class: 'row small wrap', style: { gap: '6px', marginTop: '4px' } });
        if (s.problem) meta.append(h('span', { class: 'badge warn', text: s.problem }));
        else if (s.enabled && s.next_run) meta.append(h('span', { class: 'badge', text: 'next ' + new Date(s.next_run * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) }));
        else if (!s.enabled) meta.append(h('span', { class: 'badge', text: 'paused' }));
        if (s.last_run) meta.append(h('span', { class: 'badge ' + (s.last_ok ? 'ok' : 'danger'), title: s.last_message || '', text: (s.last_ok ? 'ran ' : 'failed ') + UI.fmtAgo(s.last_run) }));
        if (s.missed_at && (!s.last_run || s.missed_at > s.last_run)) meta.append(h('span', { class: 'badge warn', title: 'The Pi was off or busy when this was due', text: 'missed ' + UI.fmtAgo(s.missed_at) }));
        info.append(meta);
        item.append(info);
        const toggle = UI.toggle(s.enabled, (on) => api.put(`/api/schedules/${s.id}`, { enabled: on }).then(draw).catch(UI.notifyError));
        toggle.title = s.enabled ? 'Pause' : 'Resume';
        const more = el(`<button class="btn ghost icon sm" aria-label="More">${icon('more')}</button>`);
        more.onclick = () => UI.menu(more, [
          { label: 'Run now', icon: 'play', onClick: () => api.post(`/api/schedules/${s.id}/run`, {}, { timeout: 75000 }).then((r) => { UI.toast(r.ok ? (r.message || 'Done') : r.error, r.ok ? 'success' : 'error'); draw(); if (window.App) App.refresh(); }).catch(UI.notifyError) },
          { label: 'Edit', icon: 'edit', onClick: () => scheduleDialog(s, ctx, draw) },
          { label: 'Duplicate', icon: 'layers', onClick: () => { const copy = JSON.parse(JSON.stringify(s)); delete copy.id; copy.name = (s.name || 'Schedule') + ' (copy)'; scheduleDialog(copy, ctx, draw, true); } },
          '-',
          { label: 'Delete', icon: 'trash', danger: true, onClick: async () => { if (await UI.confirm({ title: 'Delete schedule', message: `Delete “${s.name || whenLabel(s)}”?`, okText: 'Delete', danger: true })) api.del(`/api/schedules/${s.id}`).then(draw).catch(UI.notifyError); } },
        ]);
        item.append(toggle, more);
        list.append(item);
      }
    }
    function drawLocation(listing) {
      locCard.innerHTML = '';
      const loc = listing.location || {};
      const lat = h('input', { class: 'input mono', type: 'number', step: '0.0001', min: -90, max: 90, value: loc.lat ?? '', placeholder: 'e.g. 51.5074' });
      const lon = h('input', { class: 'input mono', type: 'number', step: '0.0001', min: -180, max: 180, value: loc.lon ?? '', placeholder: 'e.g. -0.1278' });
      const label = h('input', { class: 'input', value: loc.label || '', placeholder: 'e.g. Home', maxlength: 60 });
      const geo = el(`<button class="btn">${icon('home')} Use this device's location</button>`);
      geo.onclick = () => {
        if (!navigator.geolocation || !window.isSecureContext) return UI.toast('Your browser only shares its location with https sites. Look up your town on a map and type the numbers in instead.', 'error', 9000);
        navigator.geolocation.getCurrentPosition((p) => { lat.value = p.coords.latitude.toFixed(4); lon.value = p.coords.longitude.toFixed(4); }, (err) => UI.toast('Could not get your location: ' + err.message, 'error'));
      };
      const tz = h('select', { class: 'select' }, h('option', { value: listing.timezone, text: listing.timezone, selected: true }));
      let zonesLoaded = false;
      tz.onfocus = async () => {
        if (zonesLoaded) return; zonesLoaded = true;
        try { const r = await api.get('/api/location?zones=1'); const cur = tz.value; tz.innerHTML = ''; for (const z of r.zones) tz.append(h('option', { value: z, text: z.replace(/_/g, ' '), selected: z === cur })); if (!r.zones.includes(cur)) tz.prepend(h('option', { value: cur, text: cur, selected: true })); } catch (e) { zonesLoaded = false; }
      };
      const here = (Intl.DateTimeFormat().resolvedOptions().timeZone || '');
      const useTz = el(`<button class="btn sm ghost" type="button">${icon('clock')} Use this device's timezone${here ? ' (' + esc(here) + ')' : ''}</button>`);
      useTz.onclick = () => { if (!here) return; if (![...tz.options].some((o) => o.value === here)) tz.append(h('option', { value: here, text: here.replace(/_/g, ' ') })); tz.value = here; };
      const save = el(`<button class="btn primary" data-busy="Saving…">Save</button>`);
      save.onclick = () => UI.busy(save, (async () => {
        const payload = { lat: lat.value === '' ? null : +lat.value, lon: lon.value === '' ? null : +lon.value, label: label.value.trim() };
        if (tz.value !== listing.timezone) payload.timezone = tz.value;
        const r = await api.put('/api/location', payload);
        UI.toast(r.message || 'Saved', 'success'); draw();
      })().catch(UI.notifyError));
      const fmt = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '–');
      const sunLine = loc.lat != null ? `Today: sunrise ${fmt(listing.sun.sunrise)}, sunset ${fmt(listing.sun.sunset)}.` : 'Set a location so schedules can follow sunrise and sunset.';
      const piNow = new Date(listing.now * 1000);
      const browserOffset = -new Date().getTimezoneOffset();
      const clockNote = listing.clock_synced === false ? `<div class="alert warn">${icon('clock')} The Pi's clock is not synchronised with the internet, so it may be wrong (a Pi has no clock battery). Schedules run by this clock – check the time shown here, and the Pi's internet connection.</div>` : '';
      const tzNote = browserOffset !== listing.utc_offset_min ? `<div class="alert warn">${icon('clock')} The Pi's clock is on ${esc(listing.timezone)} time, which differs from this device. Schedules run on the Pi's time – pick your timezone below if it is wrong.</div>` : '';
      locCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Where & when' }), h('span', { class: 'hint', text: `Pi time ${piNow.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` })),
        el(`<p class="muted small">${esc(sunLine)}${listing.running ? '' : ' <b>The scheduler is not running.</b>'}</p>`),
        clockNote ? el(clockNote) : '',
        tzNote ? el(tzNote) : '',
        h('div', { class: 'form-grid' }, UI.field('Latitude', lat), UI.field('Longitude', lon), UI.field('Place name (optional)', label), UI.field('Timezone', tz, 'Changing it needs the installed service.')),
        here && here !== listing.timezone ? h('div', { class: 'row end' }, useTz) : '',
        h('div', { class: 'row wrap between' }, geo, save));
    }
    draw();
  }

  async function scheduleDialog(existing, ctx, onDone, isCopy) {
    const s = existing ? JSON.parse(JSON.stringify(existing)) : { name: '', enabled: true, when: { type: 'time', time: '07:00' }, days: [0, 1, 2, 3, 4, 5, 6], random: 0, action: { type: 'lights', target: 'all', intent: { on: true } } };
    const isEdit = existing && existing.id && !isCopy;
    const name = h('input', { class: 'input', value: s.name || '', placeholder: 'e.g. Wake-up light', maxlength: 60 });
    // when
    let kind = s.when.type === 'sun' ? s.when.event : s.when.type;
    const whenWrap = h('div', { class: 'stack' });
    const kindChips = h('div', { class: 'chips' });
    const time = h('input', { class: 'input', type: 'time', value: s.when.time || '07:00', style: { maxWidth: '160px' } });
    const off = Math.abs(s.when.offset || 0);
    const offset = h('input', { class: 'input', type: 'number', min: 0, max: 180, value: off, style: { maxWidth: '110px' } });
    const offDir = h('select', { class: 'select', style: { maxWidth: '140px' } }, h('option', { value: '-1', text: 'minutes before', selected: (s.when.offset || 0) < 0 }), h('option', { value: '1', text: 'minutes after', selected: (s.when.offset || 0) >= 0 }));
    const pad = (n) => String(n).padStart(2, '0');
    const atDate = s.when.type === 'once' ? new Date(s.when.at * 1000) : new Date(Date.now() + 3600e3);
    const at = h('input', { class: 'input', type: 'datetime-local', value: `${atDate.getFullYear()}-${pad(atDate.getMonth() + 1)}-${pad(atDate.getDate())}T${pad(atDate.getHours())}:${pad(atDate.getMinutes())}` });
    const days = new Set(s.days && s.days.length ? s.days : [0, 1, 2, 3, 4, 5, 6]);
    const dayChips = h('div', { class: 'chips' });
    const drawDays = () => { dayChips.innerHTML = ''; DAYS.forEach((d, i) => { const c = h('button', { type: 'button', class: 'chip' + (days.has(i) ? ' active' : ''), text: d }); c.onclick = () => { days.has(i) ? days.delete(i) : days.add(i); drawDays(); }; dayChips.append(c); }); for (const [label, set] of [['Every day', [0, 1, 2, 3, 4, 5, 6]], ['Weekdays', [0, 1, 2, 3, 4]], ['Weekends', [5, 6]]]) { const q = h('button', { type: 'button', class: 'chip ghost', text: label }); q.onclick = () => { days.clear(); set.forEach((x) => days.add(x)); drawDays(); }; dayChips.append(q); } };
    drawDays();
    const random = h('input', { class: 'input', type: 'number', min: 0, max: 60, value: s.random || 0, style: { maxWidth: '110px' } });
    const delAfter = h('input', { type: 'checkbox', checked: s.delete_after !== false });
    function drawWhen() {
      whenWrap.innerHTML = '';
      if (kind === 'time') whenWrap.append(UI.field('Time', time));
      else if (kind === 'sunrise' || kind === 'sunset') whenWrap.append(UI.field(`Relative to ${kind}`, h('div', { class: 'row' }, offset, offDir, h('span', { class: 'small muted', text: kind }))));
      if (kind === 'once') whenWrap.append(UI.field('Date and time', at), h('label', { class: 'check' }, delAfter, h('span', { text: 'Remove it once it has run' })));
      else whenWrap.append(UI.field('On these days', dayChips), UI.field('Vary by up to (minutes)', random, 'Runs a little earlier or later each day, so the house looks lived-in while you are away. 0 = exactly on time.'));
    }
    [['time', 'At a time', 'clock'], ['sunrise', 'Sunrise', 'sun'], ['sunset', 'Sunset', 'moon'], ['once', 'Just once', 'timer']].forEach(([k, label, ic]) => {
      const c = h('button', { type: 'button', class: 'chip' + (kind === k ? ' active' : ''), html: icon(ic) + `<span>${label}</span>` });
      c.onclick = () => { kind = k; kindChips.querySelectorAll('.chip').forEach((x) => x.classList.remove('active')); c.classList.add('active'); drawWhen(); };
      kindChips.append(c);
    });
    drawWhen();
    // action
    const action = actionEditor(s.action, ctx, { who: 'a schedule' });
    const getAction = action.getValue;
    const enabled = h('input', { type: 'checkbox', checked: s.enabled !== false });
    const body = h('div', { class: 'stack' }, UI.field('Name', name), h('h4', { text: 'When' }), kindChips, whenWrap, h('h4', { text: 'What' }), action, h('label', { class: 'check' }, enabled, h('span', { text: 'Enabled' })));
    const save = el(`<button class="btn primary" data-busy="Saving…">Save schedule</button>`);
    const m = UI.modal({ title: isEdit ? 'Edit schedule' : 'New schedule', icon: 'clock', body, footer: [save], wide: true, sticky: true });
    save.onclick = () => UI.busy(save, (async () => {
      let when;
      if (kind === 'time') { if (!time.value) throw new Error('Pick a time'); when = { type: 'time', time: time.value.slice(0, 5) }; }
      else if (kind === 'once') { const t = new Date(at.value).getTime(); if (!t) throw new Error('Pick a date and time'); if (t < Date.now()) throw new Error('That time has already passed'); when = { type: 'once', at: t / 1000 }; }
      else when = { type: 'sun', event: kind, offset: (+offset.value || 0) * +offDir.value };
      if (kind !== 'once' && !days.size) throw new Error('Pick at least one day');
      const payload = { name: name.value.trim(), enabled: enabled.checked, when, days: [...days], random: +random.value || 0, action: getAction(), delete_after: kind === 'once' ? delAfter.checked : false };
      if (isEdit) await api.put(`/api/schedules/${existing.id}`, payload); else await api.post('/api/schedules', payload);
      UI.toast('Schedule saved', 'success'); m.close(); onDone && onDone();
    })().catch(UI.notifyError));
  }

  /**
   * What a schedule or a physical button should do.  ``ctx`` = {scenes, switches}.
   * opts.allowNone adds "Nothing".  Returns an element with getValue() (null = nothing).
   */
  function actionEditor(current, ctx, opts) {
    opts = opts || {};
    current = current || (opts.allowNone ? null : { type: 'lights', target: 'all', intent: { on: true } });
    const root = h('div', { class: 'stack' });
    const actType = h('select', { class: 'select' });
    const types = [['lights', 'Change lights'], ['scene', 'Run a scene'], ['switch', 'Use a switch'], ['all_off', 'Everything off (lights and relays)']];
    if (opts.allowNone) types.unshift(['', 'Nothing']);
    types.forEach(([v, t]) => actType.append(h('option', { value: v, text: t, selected: (current ? current.type : '') === v })));
    const actWrap = h('div', { class: 'stack' });
    let getAction = () => null;
    function drawAction() {
      actWrap.innerHTML = '';
      const a = current && current.type === actType.value ? current : { type: actType.value };
      if (actType.value === '') { getAction = () => null; return; }
      if (actType.value === 'lights') {
        const tgt = Lights.targetSelect(a.target || 'all');
        const editor = Lights.intentEditor(a.intent || { on: true });
        actWrap.append(UI.field('Which lights', tgt), editor);
        getAction = () => ({ type: 'lights', target: tgt.getValue(), intent: editor.getValue() });
      } else if (actType.value === 'scene') {
        if (!ctx.scenes.length) { actWrap.append(h('div', { class: 'alert info', text: 'Make a scene first (Settings → Scenes, or “Save as scene” in the Lights panel).' })); getAction = () => { throw new Error('Choose a scene'); }; return; }
        const sel = h('select', { class: 'select' }); for (const sc of ctx.scenes) sel.append(h('option', { value: sc.id, text: sc.name, selected: sc.id === a.ref }));
        actWrap.append(UI.field('Scene', sel));
        getAction = () => ({ type: 'scene', ref: sel.value });
      } else if (actType.value === 'switch') {
        if (!ctx.switches.length) { actWrap.append(h('div', { class: 'alert info', text: 'There are no GPIO switches yet.' })); getAction = () => { throw new Error('Choose a switch'); }; return; }
        const sel = h('select', { class: 'select' }); for (const sw of ctx.switches) sel.append(h('option', { value: sw.id, text: sw.name + (sw.mode === 'momentary' ? ' (hold-to-run: off only)' : ''), selected: sw.id === a.ref }));
        const act = h('select', { class: 'select' }); [['off', 'Turn off'], ['on', 'Turn on'], ['toggle', 'Toggle'], ['pulse', 'Pulse']].forEach(([v, t]) => act.append(h('option', { value: v, text: t, selected: (a.action || 'off') === v })));
        actWrap.append(h('div', { class: 'form-grid' }, UI.field('Switch', sel), UI.field('Do', act)), h('div', { class: 'small muted', text: `For safety, hold-to-run switches (like a bed motor) can only be switched off by ${opts.who || 'an automation'}.` }));
        getAction = () => ({ type: 'switch', ref: sel.value, action: act.value });
      } else {
        actWrap.append(h('div', { class: 'small muted', text: 'Turns every light and every relay off, like the All off button.' }));
        getAction = () => ({ type: 'all_off' });
      }
    }
    actType.onchange = drawAction;
    drawAction();
    root.append(UI.field(opts.label || 'Action', actType), actWrap);
    root.getValue = () => getAction();
    return root;
  }

  // ----------------------------------------------------------- buttons
  /** Settings → Switches: push buttons wired to the Pi that run an action. */
  async function buttonsCard(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn">${icon('plus')} Add a button</button>`);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'Buttons' }), addBtn),
      el(`<p class="muted small">Wire an ordinary push button to the Pi – by the bed, by the door – and choose what a <b>tap</b> and a <b>hold</b> do: "Goodnight" on a tap, everything off on a hold. Buttons run lights, scenes and switches, but never a hold-to-run motor, and they are not emergency stops.</p>`), list);
    body.append(card);
    let ctx = { scenes: [], switches: [] };
    addBtn.onclick = () => buttonDialog(null, ctx, draw);
    async function draw() {
      let buttons = [];
      try {
        const [b, scenes, switches] = await Promise.all([api.get('/api/gpio/buttons'), api.get('/api/scenes').then((r) => r.scenes), api.get('/api/gpio/switches').then((r) => r.switches)]);
        buttons = b.buttons; ctx = { scenes, switches };
      } catch (e) { return UI.notifyError(e); }
      list.innerHTML = '';
      if (!buttons.length) list.append(h('div', { class: 'small muted', text: 'No buttons yet.' }));
      const describe = (a) => { if (!a) return 'nothing'; if (a.type === 'scene') { const sc = ctx.scenes.find((x) => x.id === a.ref); return `run “${sc ? sc.name : 'deleted scene'}”`; } if (a.type === 'switch') { const sw = ctx.switches.find((x) => x.id === a.ref); return `${a.action} ${sw ? sw.name : 'deleted switch'}`; } if (a.type === 'all_off') return 'everything off'; return `${Lights.targetLabel(a.target)}: ${Lights.describeIntent(a.intent)}`; };
      for (const b of buttons) {
        const item = h('div', { class: 'list-item' }, el(`<span class="tile-icon">${icon('hand')}</span>`),
          h('div', { class: 'grow' }, h('span', { class: 'title', text: b.name }), h('span', { class: 'sub', text: `GPIO${b.pin} · tap: ${describe(b.press)} · hold: ${describe(b.hold)}${b.error ? ' · ' + b.error : ''}` })));
        item.append(el(`<span class="status-dot ${b.pressed ? 'ok' : (b.available ? '' : 'warn')}" title="${b.pressed ? 'pressed now' : b.available ? 'ready' : esc(b.error || 'not available')}"></span>`));
        const tryIt = el(`<button class="btn sm">${icon('play')} Try</button>`);
        tryIt.onclick = () => UI.menu(tryIt, [b.press ? { label: 'Do what a tap does', icon: 'play', onClick: () => api.post(`/api/gpio/buttons/${b.id}/try`, { kind: 'press' }).then((r) => UI.toast(r.message, 'success')).catch(UI.notifyError) } : null, b.hold ? { label: 'Do what a hold does', icon: 'play', onClick: () => api.post(`/api/gpio/buttons/${b.id}/try`, { kind: 'hold' }).then((r) => UI.toast(r.message, 'success')).catch(UI.notifyError) } : null].filter(Boolean));
        const edit = el(`<button class="btn ghost icon sm" aria-label="Edit">${icon('edit')}</button>`); edit.onclick = () => buttonDialog(b, ctx, draw);
        const del = el(`<button class="btn ghost icon sm" aria-label="Delete">${icon('trash')}</button>`);
        del.onclick = async () => { if (await UI.confirm({ title: 'Remove button', message: `Remove “${b.name}”? The button itself stops doing anything.`, okText: 'Remove', danger: true })) api.del(`/api/gpio/buttons/${b.id}`).then(draw).catch(UI.notifyError); };
        item.append(tryIt, edit, del);
        list.append(item);
      }
    }
    draw();
  }

  async function buttonDialog(existing, ctx, onDone) {
    const b = existing ? JSON.parse(JSON.stringify(existing)) : { name: '', pin: null, pull: 'up', enabled: true, press: { type: 'all_off' }, hold: null };
    const name = h('input', { class: 'input', value: b.name, placeholder: 'e.g. Bedside button', maxlength: 60 });
    const pin = await Settings.pinSelect(existing ? b.pin : null);
    const pull = h('select', { class: 'select' },
      h('option', { value: 'up', text: 'Button between the pin and GND (recommended)', selected: b.pull === 'up' }),
      h('option', { value: 'down', text: 'Button between the pin and 3.3 V', selected: b.pull === 'down' }));
    const tap = actionEditor(b.press, ctx, { allowNone: true, label: 'When tapped', who: 'a button' });
    const hold = actionEditor(b.hold, ctx, { allowNone: true, label: 'When held for a second', who: 'a button' });
    const enabled = h('input', { type: 'checkbox', checked: b.enabled !== false });
    const body = h('div', { class: 'stack' }, h('div', { class: 'form-grid' }, UI.field('Name', name), UI.field('GPIO pin', pin), h('div', { class: 'full' }, UI.field('Wiring', pull))),
      h('h4', { text: 'Tap' }), tap, h('h4', { text: 'Hold' }), hold, h('label', { class: 'check' }, enabled, h('span', { text: 'Enabled' })));
    const save = el(`<button class="btn primary" data-busy="Saving…">Save button</button>`);
    const m = UI.modal({ title: existing ? 'Edit button' : 'Add a button', icon: 'hand', body, footer: [save], wide: true, sticky: true });
    save.onclick = () => UI.busy(save, (async () => {
      if (!pin.value) throw new Error('Pick a GPIO pin');
      const payload = { name: name.value.trim() || 'Button', pin: +pin.value, pull: pull.value, enabled: enabled.checked, press: tap.getValue(), hold: hold.getValue() };
      if (!payload.press && !payload.hold) throw new Error('Choose what a tap or a hold should do');
      if (existing) await api.put(`/api/gpio/buttons/${existing.id}`, payload); else await api.post('/api/gpio/buttons', payload);
      UI.toast('Button saved', 'success'); m.close(); onDone && onDone();
    })().catch(UI.notifyError));
  }

  // ------------------------------------------------------ integrations
  async function integrationsSection(body) {
    const card = h('div', { class: 'card' });
    const list = h('div', { class: 'list' });
    const addBtn = el(`<button class="btn primary">${icon('plus')} New token</button>`);
    card.append(h('div', { class: 'card-title' }, h('h3', { text: 'API tokens' }), addBtn),
      el(`<p class="muted small">A token lets another program control JimboLED – Home Assistant, an iPhone Shortcut, a script – even when the dashboard has a password. Make one per program, so you can switch one off without breaking the others. Tokens can control lights, scenes, switches and schedules, but can't change passwords, tokens or backups.</p>`), list);
    const examples = h('div', { class: 'card' });
    body.append(card, examples);
    addBtn.onclick = async () => {
      const name = await UI.prompt({ title: 'New API token', label: 'What is it for?', placeholder: 'e.g. Home Assistant', okText: 'Create token', maxlength: 40 });
      if (!name || !name.trim()) return;
      try { const r = await api.post('/api/tokens', { name: name.trim() }); showToken(r.token); draw(); } catch (e) { UI.notifyError(e); }
    };
    function showToken(token) {
      const box = h('input', { class: 'input mono', value: token, readonly: true });
      const copy = el(`<button class="btn">${icon('save')} Copy</button>`);
      copy.onclick = async () => { try { await navigator.clipboard.writeText(token); UI.toast('Copied', 'success'); } catch (e) { box.select(); document.execCommand && document.execCommand('copy'); UI.toast('Copied', 'success'); } };
      const done = el(`<button class="btn primary">I've saved it</button>`);
      const m = UI.modal({ title: 'Your new token', icon: 'key', sticky: true, body: h('div', { class: 'stack' }, el(`<div class="alert warn">${icon('alert')} Copy this now – it is only shown once. If you lose it, delete it and make a new one.</div>`), h('div', { class: 'row' }, h('div', { class: 'grow' }, box), copy)), footer: [done] });
      done.onclick = m.close;
      setTimeout(() => box.select(), 50);
      drawExamples(token);
    }
    async function draw() {
      list.innerHTML = '';
      let tokens = [];
      try { tokens = (await api.get('/api/tokens')).tokens; } catch (e) { return UI.notifyError(e); }
      if (!tokens.length) list.append(h('div', { class: 'muted small', text: 'No tokens yet.' }));
      for (const t of tokens) {
        const item = h('div', { class: 'list-item' }, el(`<span class="tile-icon">${icon('key')}</span>`),
          h('div', { class: 'grow' }, h('span', { class: 'title', text: t.name }), h('span', { class: 'sub', text: `${t.prefix}… · made ${new Date(t.created * 1000).toLocaleDateString()} · last used ${t.last_used ? UI.fmtAgo(t.last_used) : 'not since restart'}` })));
        const del = el(`<button class="btn ghost sm">${icon('trash')}<span>Delete</span></button>`);
        del.onclick = async () => { if (await UI.confirm({ title: 'Delete token', message: `Delete “${t.name}”? Anything using it stops working straight away.`, okText: 'Delete', danger: true })) api.del(`/api/tokens/${t.id}`).then(draw).catch(UI.notifyError); };
        item.append(del); list.append(item);
      }
    }
    function drawExamples(token) {
      const base = location.origin;
      const tok = token || 'YOUR_TOKEN';
      const pre = (text) => { const p = h('pre', { class: 'code' }); p.textContent = text; return p; };
      examples.innerHTML = '';
      examples.append(h('div', { class: 'card-title' }, h('h3', { text: 'Examples' })),
        h('p', { class: 'small muted', text: 'Every light red, from a terminal:' }),
        pre(`curl -X POST ${base}/api/lights/apply \\\n  -H "Authorization: Bearer ${tok}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"target": "all", "color": "#ff0000"}'`),
        h('p', { class: 'small muted', text: 'Home Assistant (configuration.yaml), then call rest_command.jimboled_preset with a preset name:' }),
        pre(`rest_command:\n  jimboled_preset:\n    url: "${base}/api/lights/apply"\n    method: POST\n    headers:\n      Authorization: "Bearer ${tok}"\n    content_type: "application/json"\n    payload: '{"target": "all", "preset": "{{ preset }}"}'`),
        el(`<p class="small muted">Other things you can send: <code>"on": false</code>, <code>"brightness": 40</code> (percent), <code>"effect": "Rainbow"</code>, <code>"kelvin": 2700</code>, <code>"target": "group:&lt;id&gt;"</code>. Scenes run with <code>POST /api/scenes/&lt;id&gt;/run</code>. The full list is in docs/API.md.</p>`));
    }
    draw();
    drawExamples();
  }

  window.Automation = { groupsSection, groupDialog, schedulesSection, scheduleDialog, integrationsSection, whenLabel, actionEditor, buttonsCard, buttonDialog };
})();
