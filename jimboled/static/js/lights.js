/* Every light at once: the Lights panel, group and "all lights" tiles, copy
   dialogs, and the light-settings editor shared by scenes and schedules.

   Everything here talks to /api/lights/*, which matches effects, palettes and
   presets by *name* on each controller, so one tap does the same thing on
   controllers that number them differently. */
(function () {
  const { h, el, esc } = UI;

  // ----------------------------------------------------------- targets
  const groups = () => (window.App && App.state.groups) || [];
  const devices = () => (window.App && App.state.devices) || [];
  function targetKey(t) {
    if (!t || t === 'all' || t.all) return 'all';
    if (typeof t === 'string') return t;
    if (t.group) return 'group:' + t.group;
    if (t.devices) return 'devices:' + t.devices.join(',');
    return 'all';
  }
  /** API form of a target key. */
  function targetValue(key) {
    if (!key || key === 'all') return 'all';
    if (key.startsWith('devices:')) return { devices: key.slice(8).split(',').filter(Boolean) };
    return key;
  }
  function targetLabel(t) {
    const key = targetKey(t);
    if (key === 'all') return 'All lights';
    if (key.startsWith('group:')) { const g = groups().find((x) => x.id === key.slice(6)); return g ? g.name : 'Deleted group'; }
    const ids = key.startsWith('device:') ? [key.slice(7)] : key.slice(8).split(',');
    const names = devices().filter((d) => ids.includes(d.id)).map((d) => d.name);
    return names.length === 1 ? names[0] : `${names.length} lights`;
  }
  /** Controller objects (from the live state) a target covers, enabled only. */
  function devicesFor(t) {
    const key = targetKey(t);
    const all = devices().filter((d) => d.enabled !== false);
    if (key === 'all') return all;
    if (key.startsWith('group:')) { const g = groups().find((x) => x.id === key.slice(6)); const m = new Set(g ? g.devices : []); return all.filter((d) => m.has(d.id)); }
    const ids = new Set(key.startsWith('device:') ? [key.slice(7)] : key.slice(8).split(','));
    return all.filter((d) => ids.has(d.id));
  }
  function catalogUrl(t) {
    const v = targetValue(targetKey(t));
    if (typeof v === 'object') return '/api/lights/catalog?devices=' + encodeURIComponent(v.devices.join(','));
    return '/api/lights/catalog?target=' + encodeURIComponent(v);
  }
  const catalogCache = new Map();
  async function catalog(t, maxAgeMs) {
    const key = targetKey(t);
    const hit = catalogCache.get(key);
    if (hit && Date.now() - hit.at < (maxAgeMs ?? 30000)) return hit.data;
    const data = await api.get(catalogUrl(t));
    catalogCache.set(key, { at: Date.now(), data });
    return data;
  }

  // ------------------------------------------------------------- apply
  /** Send one intent to a target.  Resolves to the server's summary (or null on error). */
  async function apply(target, intent, opts) {
    opts = opts || {};
    try {
      // Only the lights panel offers Undo; elsewhere the server needn't
      // remember how the lights looked.
      const r = await api.post('/api/lights/apply', { target: targetValue(targetKey(target)), intent, ...(opts.undo ? {} : { no_undo: true }) });
      if (r.devices && window.App && App.setDevices) App.setDevices(r.devices);
      if (opts.status) opts.status(r);
      if (!r.ok && !opts.quiet) UI.toast(r.message, 'error', 9000);
      else if (opts.toast) UI.toast(r.message, 'success');
      return r;
    } catch (err) { UI.notifyError(err); return null; }
  }

  // ------------------------------------------------------------- tiles
  /** Body for a group or "all lights" tile.  Returns an element with .update(). */
  function tileBody(target, size, opts) {
    opts = opts || {};
    const root = h('div', { class: 'tile-body' });
    const power = UI.toggle(false, (on) => apply(target, { on }), 'lg');
    const sendBri = UI.throttle((v) => apply(target, { bri: v, on: true }, { quiet: true }), 450);
    const bri = UI.slider({ icon: 'sun', min: 1, max: 255, value: 128, format: (v) => Math.round(v / 2.55) + '%', onInput: sendBri, onChange: (v) => apply(target, { bri: v, on: true }) });
    root.append(h('div', { class: 'row' }, power, h('div', { class: 'grow' }, bri)));
    let swatches = null, presets = null;
    if (size !== 's') {
      swatches = h('div', { class: 'swatches wrap' });
      const favs = (window.App && App.state.dashboard && App.state.dashboard.favorite_colors) || [];
      const picks = [...favs, ...['#ff0000', '#ff8000', '#ffd000', '#00ff40', '#00c8ff', '#2040ff', '#b000ff', '#ff0080', '#ffd8a8']].filter((c, i, a) => a.indexOf(c) === i).slice(0, size === 'm' ? 7 : 12);
      for (const hx of picks) {
        const s = h('button', { class: 'swatch', style: { background: hx }, title: `Make ${opts.label || 'them'} ${hx}`, 'aria-label': `Colour ${hx}` });
        s.onclick = (e) => { e.stopPropagation(); apply(target, { color: hx }); };
        swatches.append(s);
      }
      if (size === 'm') root.append(swatches);
      else {
        // Wide tiles have room to say where the rest of the controls are.
        const more = el(`<button class="btn sm ghost" title="Effects, presets, whites and more">${icon('sliders')}<span>More</span></button>`);
        more.onclick = (e) => { e.stopPropagation(); open(target); };
        root.append(h('div', { class: 'row between wrap' }, swatches, more));
      }
    }
    if (size === 'l' || size === 'xl') { presets = h('div', { class: 'chips' }); root.append(presets); }
    let presetsAt = 0;
    async function loadPresets() {
      presetsAt = Date.now();
      try {
        const cat = await catalog(target, 60000);
        presets.innerHTML = '';
        const list = cat.presets.filter((p) => !p.playlist).slice(0, 8);
        for (const p of list) {
          const c = h('button', { class: 'chip', title: p.count < cat.online ? `On ${p.count} of ${cat.online} lights` : 'On every light' }, p.name, p.count < cat.online ? h('span', { class: 'count', text: `${p.count}/${cat.online}` }) : null);
          c.onclick = (e) => { e.stopPropagation(); apply(target, { preset: p.name }); };
          presets.append(c);
        }
        if (!list.length) presets.append(h('span', { class: 'small faint', text: 'No presets saved on these controllers yet' }));
      } catch (e) { /* offline blip: try again next update */ presetsAt = 0; }
    }
    root.update = () => {
      const list = devicesFor(target);
      const online = list.filter((d) => d.online);
      const lit = online.filter((d) => d.state && d.state.on);
      power.setOn(lit.length > 0); power.disabled = !online.length;
      bri.input.disabled = !online.length;
      if (lit.length) bri.setValue(Math.round(lit.reduce((a, d) => a + (d.state.bri || 0), 0) / lit.length));
      if (presets && Date.now() - presetsAt > 60000) loadPresets();
      return { total: list.length, online: online.length, on: lit.length };
    };
    return root;
  }

  // ------------------------------------------------------------- panel
  let openPanel = null;
  async function open(target) {
    if (openPanel) openPanel.close();
    let key = targetKey(target || 'all');
    let cat = null;
    const opts = { transition: null, sync: false };
    const status = h('div', { class: 'lights-status small muted' });
    const result = h('div', { class: 'lights-result small' });
    const targetBar = h('div', { class: 'chips target-bar' });
    const content = h('div', { class: 'stack' });
    let tab = 'colour';
    const tabsEl = UI.tabs([{ id: 'colour', label: 'Colour' }, { id: 'presets', label: 'Presets' }, { id: 'effects', label: 'Effects' }, { id: 'more', label: 'Timer & more' }], tab, (id) => { tab = id; draw(); });

    // power + brightness, always visible
    const onBtn = el(`<button class="btn">${icon('sun')}<span>On</span></button>`);
    const offBtn = el(`<button class="btn">${icon('moon')}<span>Off</span></button>`);
    onBtn.onclick = () => send({ on: true });
    offBtn.onclick = () => send({ on: false });
    // One drag of the slider is one undo step, and so is a burst of arrow-key
    // presses: the step ends a moment after the last change, when the next
    // drag starts, or when anything else is changed.  (A drag let go where it
    // started fires no "change" at all.)
    let briGesture = null, briIdle = null;
    const endBriGesture = () => { clearTimeout(briIdle); briIdle = null; briGesture = null; };
    // A throttled send still queued when its step has ended (by Undo, say)
    // is dropped: it would put the dragged brightness back.
    const sendBri = UI.throttle((v, g) => { if (g === briGesture) send({ bri: v, on: true }, g, true); }, 450);
    const bri = UI.slider({ label: 'Brightness', min: 1, max: 255, value: 128, format: (v) => Math.round(v / 2.55) + '%',
      onInput: (v) => { if (!briGesture) briGesture = newGesture(); sendBri(v, briGesture); },
      onChange: (v) => send({ bri: v, on: true }, briGesture) });
    bri.input.addEventListener('change', () => { clearTimeout(briIdle); briIdle = setTimeout(endBriGesture, 1500); });  // after onChange
    bri.input.addEventListener('pointerdown', endBriGesture);
    bri.input.addEventListener('keydown', () => { if (!briIdle) endBriGesture(); });
    const body = h('div', { class: 'stack lights-panel' }, targetBar, status,
      h('div', { class: 'row wrap' }, h('div', { class: 'btn-group' }, onBtn, offBtn), h('div', { class: 'grow', style: { minWidth: '200px' } }, bri)),
      result, tabsEl, content);
    const sceneBtn = el(`<button class="btn">${icon('save')}<span>Save as scene</span></button>`);
    sceneBtn.onclick = () => saveSceneDialog(targetValue(key));
    const copyBtn = el(`<button class="btn">${icon('layers')}<span>Copy a light's look</span></button>`);
    copyBtn.onclick = () => copyFromDialog(targetValue(key), () => refreshState());
    const m = UI.modal({ title: targetLabel(key), icon: 'sun', body, wide: true, footer: [copyBtn, sceneBtn], onClose: () => { openPanel = null; clearInterval(poll); } });
    openPanel = m;
    const poll = setInterval(() => { if (!document.hidden) refreshState(); }, 3000);

    function withOpts(intent) {
      const out = { ...intent };
      if (opts.transition != null) out.transition = opts.transition;
      if (opts.sync && (intent.effect || intent.preset)) out.sync = true;
      return out;
    }
    // Undo goes back one step.  A step ("gesture") is one tap, one slider
    // drag or one colour-picker session; only its first request asks the
    // server to remember the lights, and Undo puts them back to before it.
    let undo = null, gestures = 0, latest = null;
    const newGesture = () => ({ n: ++gestures, started: false });
    async function send(intent, gesture, quiet) {
      const g = gesture || newGesture();
      const first = !g.started;
      g.started = true;
      if (first) {
        latest = g;  // the most recently *started* step
        if (g !== briGesture) endBriGesture();  // anything else ends a brightness step
      }
      const r = await apply(targetValue(key), withOpts(intent), { quiet: true, status: showResult, undo: first });
      // Replies can come back out of order: only the newest step's undo counts,
      // and only from the reply that carries it (a used one never comes back).
      if (r && r.undo && first && latest === g) undo = r.undo;
      if (r) drawUndo();
      if (r && !quiet) refreshState();
      return r;
    }
    function showResult(r) {
      result.className = 'lights-result small ' + (r.ok ? 'ok' : 'warn');
      result.innerHTML = (r.ok ? icon('check') : icon('alert')) + '<span></span>';
      result.querySelector('span').textContent = r.message;
    }
    function drawUndo() {
      if (!undo || result.querySelector('.undo')) return;
      const b = el(`<button class="btn sm ghost undo">${icon('rotate')}<span>Undo</span></button>`);
      b.onclick = async () => {
        const k = undo; undo = null; b.remove(); endBriGesture();  // the next change is a new step
        try { const r = await api.post(`/api/lights/undo/${k}`); if (r.devices && App.setDevices) App.setDevices(r.devices); showResult(r); refreshState(); } catch (e) { UI.notifyError(e); }
      };
      result.append(b);
    }
    function drawTargets() {
      targetBar.innerHTML = '';
      const add = (k, label, ic) => {
        const c = h('button', { class: 'chip' + (k === key ? ' active' : ''), html: (ic ? icon(ic) : '') + '<span></span>' });
        c.querySelector('span').textContent = label;
        c.onclick = () => { key = k; m.setTitle(targetLabel(key)); drawTargets(); load(); };
        targetBar.append(c);
      };
      add('all', 'All lights', 'sun');
      for (const g of groups()) add('group:' + g.id, g.name, g.icon || 'home');
      if (key.startsWith('devices:') || key.startsWith('device:')) add(key, targetLabel(key), 'bulb');
      const choose = el(`<button class="chip">${icon('plus')}<span>Choose lights…</span></button>`);
      choose.onclick = () => chooseDevicesDialog(devicesFor(key).map((d) => d.id), (ids) => { key = ids.length === devices().length ? 'all' : 'devices:' + ids.join(','); m.setTitle(targetLabel(key)); drawTargets(); load(); });
      targetBar.append(choose);
    }
    function refreshState() {
      const list = devicesFor(key);
      const online = list.filter((d) => d.online), lit = online.filter((d) => d.state && d.state.on);
      const parts = [`${list.length} light${list.length === 1 ? '' : 's'}`];
      if (online.length < list.length) parts.push(`${list.length - online.length} offline`);
      parts.push(lit.length ? `${lit.length} on` : 'all off');
      if (cat && cat.power_ma) parts.push(`≈ ${(cat.power_ma / 1000).toFixed(1)} A`);
      status.textContent = parts.join(' · ');
      if (lit.length) bri.setValue(Math.round(lit.reduce((a, d) => a + (d.state.bri || 0), 0) / lit.length));
      onBtn.classList.toggle('active', lit.length > 0 && lit.length === online.length);
      offBtn.classList.toggle('active', online.length > 0 && !lit.length);
    }
    async function load() {
      content.innerHTML = '';
      content.append(el(`<div class="row small muted">${icon('refresh', 'spin')} <span>Loading what these lights can do…</span></div>`));
      refreshState();
      try { cat = await catalog(targetValue(key), 0); } catch (e) { cat = null; content.innerHTML = ''; content.append(el(`<div class="alert warn">${esc(e.message)}</div>`)); return; }
      refreshState();
      draw();
    }
    function draw() {
      content.innerHTML = '';
      if (!cat) return;
      if (!cat.total) { content.append(el(`<div class="empty">${icon('bulb')}<h3>No lights here</h3><p>Add controllers in Settings, or put some in this group.</p></div>`)); return; }
      if (!cat.online) { content.append(el(`<div class="alert warn">${icon('wifiOff')} None of these lights are online right now.</div>`)); return; }
      if (tab === 'colour') content.append(colourTab());
      else if (tab === 'presets') content.append(presetsTab());
      else if (tab === 'effects') content.append(effectsTab());
      else content.append(moreTab());
    }
    const partial = (n) => (n < cat.online ? h('span', { class: 'count', title: `Only ${n} of the ${cat.online} lights have this`, text: `${n}/${cat.online}` }) : null);

    function colourTab() {
      const root = h('div', { class: 'stack' });
      const favs = (App.state.dashboard && App.state.dashboard.favorite_colors) || [];
      const swatchGrid = (list) => {
        const g = h('div', { class: 'preset-colors' });
        for (const hx of list) { const b = h('button', { class: 'swatch', style: { background: hx }, title: hx, 'aria-label': `Colour ${hx}` }); b.onclick = () => send({ color: hx, effect: 'Solid' }); g.append(b); }
        return g;
      };
      if (favs.length) root.append(h('h4', { text: 'Favourites' }), swatchGrid(favs));
      root.append(h('h4', { text: 'Colours' }), swatchGrid(Color.QUICK.filter((c) => c !== '#000000')));
      const custom = el(`<button class="btn">${icon('palette')}<span>Pick any colour…</span></button>`);
      custom.onclick = () => customColourDialog();
      root.append(h('div', { class: 'row wrap' }, custom, h('span', { class: 'small muted', text: 'Tapping a colour switches the lights to a plain colour.' })));
      // Whites: CCT strips get a real temperature, white-channel strips their
      // white LEDs, RGB-only strips the nearest mix.
      const whites = [['Candle', 2000], ['Warm', 2700], ['Soft', 3500], ['Neutral', 4500], ['Daylight', 5500], ['Cool', 6500]];
      const wrow = h('div', { class: 'chips' });
      for (const [label, k] of whites) {
        const rgb = Color.kelvinToRgb(k);
        const b = h('button', { class: 'chip', title: `${k} K` }, h('span', { class: 'dot', style: { background: Color.rgbToHex(rgb) } }), label);
        b.onclick = () => send({ kelvin: k, effect: 'Solid' });
        wrow.append(b);
      }
      root.append(h('h4', { text: 'Whites' }), wrow);
      if (!cat.caps.white && !cat.caps.cct) root.append(h('div', { class: 'small faint', text: 'These strips have no separate white LEDs, so whites are mixed from red, green and blue.' }));
      return root;
    }
    function customColourDialog() {
      let last = null;
      const g = newGesture();  // everything picked in this dialog is one undo step
      const sendLive = UI.throttle((rgbw, cct) => send({ color: Color.rgbToHex(rgbw), ...(cat.caps.white ? { white: rgbw[3] } : {}), ...(cat.caps.cct ? { cct } : {}), best_effort: true }, g, true), 450);
      const picker = Color.picker({ rgb: [255, 140, 0], hasWhite: cat.caps.white, hasCct: cat.caps.cct, onChange: (rgbw, cct) => { last = [rgbw, cct]; sendLive(rgbw, cct); } });
      const fav = el(`<button class="btn">${icon('star')}<span>Add to favourites</span></button>`);
      fav.onclick = async () => { const hx = Color.rgbToHex((last && last[0]) || picker.getValue().rgbw); try { await addFavourite(hx); UI.toast(`${hx} added to favourites`, 'success'); } catch (e) { UI.notifyError(e); } };
      const done = el(`<button class="btn primary">Done</button>`);
      const dm = UI.modal({ title: 'Pick a colour', icon: 'palette', body: picker, footer: [fav, done] });
      done.onclick = async () => { if (last) await send({ color: Color.rgbToHex(last[0]), ...(cat.caps.white ? { white: last[0][3] } : {}), ...(cat.caps.cct ? { cct: last[1] } : {}), best_effort: true }, g); dm.close(); draw(); };
    }
    function presetsTab() {
      const root = h('div', { class: 'stack' });
      if (!cat.presets.length) { root.append(el(`<div class="empty">${icon('star')}<h3>No presets yet</h3><p>Presets are saved on each controller (open a light and use its Presets tab). Give presets the same name on different controllers and they show up here together.</p></div>`)); return root; }
      root.append(h('p', { class: 'small muted', text: 'Each light uses its own preset with this name. Numbers like 2/3 mean only some of the lights have it; the others are left alone.' }));
      const grid = h('div', { class: 'option-grid', style: { maxHeight: 'none' } });
      for (const p of cat.presets) {
        const o = h('button', { class: 'option' });
        const meta = h('div', { class: 'meta' });
        if (p.playlist) meta.append(h('span', { text: 'playlist' }));
        if (p.quick_label) meta.append(h('span', { text: p.quick_label }));
        const part = partial(p.count); if (part) meta.append(part);
        o.append(h('span', { class: 'name', text: p.name }), meta);
        o.title = 'On: ' + p.devices.join(', ');
        o.onclick = () => send({ preset: p.name });
        grid.append(o);
      }
      root.append(grid);
      return root;
    }
    function effectsTab() {
      const root = h('div', { class: 'stack' });
      const search = h('input', { class: 'input', placeholder: 'Search effects…', type: 'search' });
      const grid = h('div', { class: 'option-grid' });
      const drawFx = () => {
        grid.innerHTML = '';
        const q = search.value.trim().toLowerCase();
        for (const e of cat.effects) {
          if (q && !e.name.toLowerCase().includes(q)) continue;
          const o = h('button', { class: 'option' });
          const meta = h('div', { class: 'meta' });
          const f = e.flags || {};
          if (f.two_d) meta.append(h('span', { text: '2D' }));
          if (f.audio_volume || f.audio_frequency) meta.append(h('span', { text: '♪' }));
          const part = partial(e.count); if (part) meta.append(part);
          o.append(h('span', { class: 'name', text: e.name }), meta);
          o.onclick = () => send({ effect: e.name });
          grid.append(o);
        }
        if (!grid.children.length) grid.append(h('div', { class: 'muted small', text: 'No effects match' }));
      };
      search.oninput = UI.debounce(drawFx, 80);
      drawFx();
      const palSearch = h('input', { class: 'input', placeholder: 'Search palettes…', type: 'search' });
      const pgrid = h('div', { class: 'option-grid' });
      const drawPal = () => {
        pgrid.innerHTML = '';
        const q = palSearch.value.trim().toLowerCase();
        for (const p of cat.palettes) {
          if (q && !p.name.toLowerCase().includes(q)) continue;
          const o = h('button', { class: 'option' }, h('span', { class: 'name', text: p.name }));
          const grad = Device.paletteGradient && Device.paletteGradient(p.stops);
          o.append(h('div', { class: 'pal-preview', style: grad ? { background: grad } : { background: 'var(--surface-3)' } }));
          const part = partial(p.count); if (part) o.append(h('div', { class: 'meta' }, part));
          o.onclick = () => send({ palette: p.name });
          pgrid.append(o);
        }
      };
      palSearch.oninput = UI.debounce(drawPal, 80);
      drawPal();
      const speed = UI.slider({ label: 'Speed', min: 0, max: 255, value: 128, onChange: (v) => send({ speed: v }) });
      const intensity = UI.slider({ label: 'Intensity', min: 0, max: 255, value: 128, onChange: (v) => send({ intensity: v }) });
      root.append(search, grid, h('div', { class: 'form-grid' }, speed, intensity), h('h4', { text: 'Palette' }), palSearch, pgrid);
      return root;
    }
    function moreTab() {
      const root = h('div', { class: 'stack' });
      // Sleep: WLED's own nightlight fades each controller out by itself.
      const fade = h('div', { class: 'chips' });
      for (const min of [5, 15, 30, 60, 90]) {
        const b = h('button', { class: 'chip', text: `${min} min` });
        b.onclick = () => send({ nightlight: { on: true, minutes: min, mode: 'fade' } }).then((r) => r && r.ok && UI.toast(`Fading out over ${min} minutes`, 'success'));
        fade.append(b);
      }
      const cancelFade = el(`<button class="btn sm ghost">${icon('x')}<span>Stop fading</span></button>`);
      cancelFade.onclick = () => send({ nightlight: { on: false } });
      root.append(h('div', { class: 'card flat' }, h('h4', { text: 'Fade to off' }), h('p', { class: 'small muted', text: 'The lights dim slowly and switch off by themselves – nice for falling asleep.' }), h('div', { class: 'row wrap' }, fade, cancelFade)));
      // Off timer: a one-off schedule on the Pi.
      const timerChips = h('div', { class: 'chips' });
      for (const [label, min] of [['15 min', 15], ['30 min', 30], ['1 hour', 60], ['2 hours', 120], ['4 hours', 240]]) {
        const b = h('button', { class: 'chip', text: label });
        b.onclick = async () => {
          try {
            await api.post('/api/timers', { minutes: min, name: `${targetLabel(key)} off`, action: { type: 'lights', target: targetValue(key), intent: { on: false } } });
            UI.toast(`${targetLabel(key)} will switch off in ${label}`, 'success'); drawTimers();
          } catch (e) { UI.notifyError(e); }
        };
        timerChips.append(b);
      }
      const timers = h('div', { class: 'list' });
      async function drawTimers() {
        timers.innerHTML = '';
        let r; try { r = await api.get('/api/schedules'); } catch (e) { return; }
        for (const s of r.schedules.filter((x) => x.when.type === 'once' && x.enabled && x.next_run)) {
          const item = h('div', { class: 'list-item' }, el(`<span class="tile-icon">${icon('timer')}</span>`), h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name || 'Timer' }), h('span', { class: 'sub', text: `at ${new Date(s.next_run * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · in ${UI.fmtDuration(s.next_run - Date.now() / 1000)}` })));
          const del = el(`<button class="btn ghost sm">${icon('x')}<span>Cancel</span></button>`);
          del.onclick = () => api.del(`/api/schedules/${s.id}`).then(drawTimers).catch(UI.notifyError);
          item.append(del); timers.append(item);
        }
      }
      drawTimers();
      root.append(h('div', { class: 'card flat' }, h('h4', { text: 'Switch off later' }), h('p', { class: 'small muted', text: 'JimboLED switches these lights off at the time you pick, even if this page is closed.' }), timerChips, timers));
      // Options for every change made from this panel.
      const trans = UI.slider({ label: 'Fade time for changes', min: 0, max: 100, value: opts.transition == null ? 7 : Math.round(opts.transition * 10), format: (v) => (v / 10).toFixed(1) + ' s', onChange: (v) => { opts.transition = v / 10; } });
      const sync = UI.toggle(opts.sync, (on) => { opts.sync = on; });
      root.append(h('div', { class: 'card flat' }, h('h4', { text: 'Options' }), trans,
        h('div', { class: 'row between' }, h('div', {}, h('div', { text: 'Keep animations in step' }), h('div', { class: 'small muted', text: 'Restarts effects together so the lights move as one.' })), sync)));
      return root;
    }
    drawTargets();
    load();
    return m;
  }

  function chooseDevicesDialog(selected, onDone) {
    const chosen = new Set(selected);
    const chips = h('div', { class: 'chips' });
    for (const d of devices().filter((x) => x.enabled !== false)) {
      const c = h('button', { class: 'chip' + (chosen.has(d.id) ? ' active' : ''), html: icon(d.icon || 'bulb') + '<span></span>' });
      c.querySelector('span').textContent = d.name + (d.online ? '' : ' (offline)');
      c.onclick = () => { chosen.has(d.id) ? chosen.delete(d.id) : chosen.add(d.id); c.classList.toggle('active', chosen.has(d.id)); };
      chips.append(c);
    }
    const ok = el(`<button class="btn primary">Use these lights</button>`);
    const m = UI.modal({ title: 'Choose lights', icon: 'bulb', body: h('div', { class: 'stack' }, h('p', { class: 'small muted', text: 'Tap the lights to control together.' }), chips), footer: [ok] });
    ok.onclick = () => { if (!chosen.size) return UI.toast('Pick at least one light', 'error'); m.close(); onDone([...chosen]); };
  }

  async function addFavourite(hx) {
    const dash = (await api.get('/api/dashboard')).dashboard;
    const favs = (dash.favorite_colors || []).filter((c) => c !== hx.toLowerCase());
    favs.unshift(hx.toLowerCase());
    await api.put('/api/dashboard', { favorite_colors: favs.slice(0, 24) });
    if (window.App) App.refresh(true);
  }

  // ------------------------------------------------------ scenes & copy
  /** Capture the target's current look and save it as a scene. */
  async function saveSceneDialog(target) {
    let cap;
    try { cap = await api.post('/api/lights/capture', { target }); } catch (e) { return UI.notifyError(e); }
    const name = await UI.prompt({ title: 'Save as scene', label: 'Scene name', placeholder: 'e.g. Movie night', okText: 'Save scene', hint: `Saves exactly how ${cap.captured} light${cap.captured === 1 ? '' : 's'} look right now. Run the scene to bring the look back.` });
    if (!name || !name.trim()) return;
    try {
      await api.post('/api/scenes', { name: name.trim(), icon: 'sparkles', actions: cap.actions.map(({ label, ...a }) => ({ ...a, label })) });
      UI.toast(`Scene “${name.trim()}” saved – it's on your dashboard`, 'success');
      if (window.App) App.refresh(true);
    } catch (e) { UI.notifyError(e); }
  }

  /** Pick a light whose look should be copied onto ``target``. */
  function copyFromDialog(target, onDone) {
    const list = devices().filter((d) => d.online);
    if (!list.length) return UI.toast('No lights are online to copy from', 'error');
    const sel = h('select', { class: 'select' });
    for (const d of list) sel.append(h('option', { value: d.id, text: d.name }));
    const bri = h('input', { type: 'checkbox', checked: true });
    const go = el(`<button class="btn primary" data-busy="Copying…">Copy look</button>`);
    const m = UI.modal({ title: `Copy a look to ${targetLabel(target)}`, icon: 'layers', body: h('div', { class: 'stack' }, UI.field('Make them look like', sel, 'Colours, effect, palette, speed and brightness are copied by name.'), h('label', { class: 'check' }, bri, h('span', { text: 'Copy brightness and on/off too' }))), footer: [go] });
    go.onclick = () => UI.busy(go, (async () => {
      const r = await api.post(`/api/devices/${sel.value}/copy`, { target: targetValue(targetKey(target)), include_brightness: bri.checked });
      if (r.devices && window.App && App.setDevices) App.setDevices(r.devices);
      UI.toast(r.message, r.ok ? 'success' : 'error', r.ok ? 3500 : 9000); m.close(); onDone && onDone();
    })().catch(UI.notifyError));
  }

  /** Target picker for "copy to": everything except ``excludeId``. */
  function copyTargets(excludeId, onChange) {
    const chosen = new Set();
    const wrap = h('div', { class: 'stack' });
    const chips = h('div', { class: 'chips' });
    const others = devices().filter((d) => d.id !== excludeId && d.enabled !== false);
    const allChip = h('button', { class: 'chip', html: icon('sun') + '<span>Every other light</span>' });
    allChip.onclick = () => { const all = others.every((d) => chosen.has(d.id)); others.forEach((d) => (all ? chosen.delete(d.id) : chosen.add(d.id))); sync(); };
    chips.append(allChip);
    for (const g of groups()) {
      const gc = h('button', { class: 'chip', html: icon(g.icon || 'home') + '<span></span>' });
      gc.querySelector('span').textContent = g.name;
      gc.onclick = () => { const ids = g.devices.filter((id) => id !== excludeId && others.some((d) => d.id === id)); const all = ids.every((id) => chosen.has(id)); ids.forEach((id) => (all ? chosen.delete(id) : chosen.add(id))); sync(); };
      chips.append(gc);
    }
    const devChips = h('div', { class: 'chips' });
    for (const d of others) {
      const c = h('button', { class: 'chip', dataset: { id: d.id }, html: icon(d.icon || 'bulb') + '<span></span>' });
      c.querySelector('span').textContent = d.name + (d.online ? '' : ' (offline)');
      c.onclick = () => { chosen.has(d.id) ? chosen.delete(d.id) : chosen.add(d.id); sync(); };
      devChips.append(c);
    }
    function sync() { devChips.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', chosen.has(c.dataset.id))); allChip.classList.toggle('active', others.length > 0 && others.every((d) => chosen.has(d.id))); onChange && onChange([...chosen]); }
    wrap.append(chips, devChips);
    wrap.getValue = () => [...chosen];
    if (!others.length) wrap.append(h('div', { class: 'alert info', text: 'Add another controller first – there is nothing to copy to yet.' }));
    return wrap;
  }

  function copyLookDialog(d) {
    const targets = copyTargets(d.id);
    const bri = h('input', { type: 'checkbox', checked: true });
    const go = el(`<button class="btn primary" data-busy="Copying…">Copy look</button>`);
    const m = UI.modal({ title: `Copy ${d.name}'s look`, icon: 'layers', body: h('div', { class: 'stack' }, h('p', { class: 'small muted', text: 'Makes the lights you pick look like this one: colours, effect, palette and speed, matched by name on each controller.' }), targets, h('label', { class: 'check' }, bri, h('span', { text: 'Copy brightness and on/off too' }))), footer: [go], wide: true });
    go.onclick = () => UI.busy(go, (async () => {
      const ids = targets.getValue(); if (!ids.length) throw new Error('Pick at least one light to copy to');
      const r = await api.post(`/api/devices/${d.id}/copy`, { target: { devices: ids }, include_brightness: bri.checked });
      if (r.devices && window.App && App.setDevices) App.setDevices(r.devices);
      UI.toast(r.message, r.ok ? 'success' : 'error', r.ok ? 3500 : 9000); m.close();
    })().catch(UI.notifyError));
  }

  function copyPresetDialog(d, p) {
    const targets = copyTargets(d.id);
    const overwrite = h('input', { type: 'checkbox', checked: true });
    const go = el(`<button class="btn primary" data-busy="Copying…">Copy preset</button>`);
    const m = UI.modal({ title: `Copy “${p.name}” to other lights`, icon: 'star', body: h('div', { class: 'stack' }, h('p', { class: 'small muted', text: 'Saves this preset on the lights you pick, under the same name, so “All lights → Presets” can switch them together. Effects and palettes are matched by name.' }), targets, h('label', { class: 'check' }, overwrite, h('span', { text: 'Replace a preset that already has this name' }))), footer: [go], wide: true });
    go.onclick = () => UI.busy(go, (async () => {
      const ids = targets.getValue(); if (!ids.length) throw new Error('Pick at least one light to copy to');
      const r = await api.post(`/api/devices/${d.id}/presets/${p.id}/copy`, { target: { devices: ids }, overwrite: overwrite.checked });
      catalogCache.clear();
      UI.toast(r.message, r.ok ? 'success' : 'error', r.ok ? 3500 : 9000); m.close();
    })().catch(UI.notifyError));
  }

  // ------------------------------------- editors for scenes & schedules
  /** <select> of all lights / groups / single controllers. getValue() → API target. */
  function targetSelect(current) {
    const sel = h('select', { class: 'select' });
    const cur = targetKey(current);
    sel.append(h('option', { value: 'all', text: 'All lights', selected: cur === 'all' }));
    if (groups().length) { const og = h('optgroup', { label: 'Groups' }); for (const g of groups()) og.append(h('option', { value: 'group:' + g.id, text: g.name, selected: cur === 'group:' + g.id })); sel.append(og); }
    const devs = devices();
    if (devs.length) { const od = h('optgroup', { label: 'One light' }); for (const d of devs) od.append(h('option', { value: 'devices:' + d.id, text: d.name, selected: cur === 'devices:' + d.id || cur === 'device:' + d.id })); sel.append(od); }
    if (cur.startsWith('devices:') && cur.includes(',')) sel.append(h('option', { value: cur, text: targetLabel(cur), selected: true }));
    // A light or group that has since been removed must not silently become
    // "All lights" (the browser's fallback when nothing matches): say so, and
    // make the person choose.
    const wanted = cur.startsWith('device:') ? 'devices:' + cur.slice(7) : cur;
    if (![...sel.options].some((o) => o.value === wanted)) {
      sel.prepend(h('option', { value: '', selected: true, text: cur.startsWith('group:') ? 'Deleted group – choose which lights' : 'Removed light – choose which lights' }));
      sel.value = '';
    }
    sel.getValue = () => { if (!sel.value) throw new Error('Choose which lights this should change'); return targetValue(sel.value); };
    return sel;
  }

  /** A compact light-settings form.  getValue() → intent (throws if empty). */
  function intentEditor(intent) {
    intent = intent || {};
    const root = h('div', { class: 'form-grid intent-editor' });
    const power = h('select', { class: 'select' });
    const curOn = intent.on === undefined ? '' : intent.on === 't' ? 't' : intent.on ? 'on' : 'off';
    [['', 'Leave as is'], ['on', 'Turn on'], ['off', 'Turn off'], ['t', 'Toggle']].forEach(([v, t]) => power.append(h('option', { value: v, text: t, selected: curOn === v })));
    const wakeTarget = intent.nightlight && intent.nightlight.on && intent.nightlight.tbri ? intent.nightlight.tbri : null;
    const briValue = wakeTarget ? Math.round(wakeTarget / 2.55) : (intent.bri ? Math.round(intent.bri / 2.55) : '');
    const bri = h('input', { class: 'input', type: 'number', min: 1, max: 100, placeholder: 'leave as is', value: briValue });
    const useCol = h('input', { type: 'checkbox', checked: !!(intent.colors && intent.colors[0]) });
    const col = h('input', { class: 'color-input', type: 'color', value: intent.colors && intent.colors[0] ? Color.rgbToHex(intent.colors[0]) : '#ff8000' });
    col.oninput = () => { useCol.checked = true; };
    const preset = h('input', { class: 'input', list: 'jl-preset-names', placeholder: 'none', value: intent.preset || '', maxlength: 64 });
    const effect = h('input', { class: 'input', list: 'jl-effect-names', placeholder: 'leave as is', value: intent.effect || '', maxlength: 64 });
    const palette = h('input', { class: 'input', list: 'jl-palette-names', placeholder: 'leave as is', value: intent.palette || '', maxlength: 64 });
    const trans = h('input', { class: 'input', type: 'number', min: 0, max: 60, step: 0.1, placeholder: 'default', value: intent.transition ?? '' });
    // Slow fades run on the controllers themselves (WLED's nightlight), so
    // they finish even if the Pi or the Wi-Fi goes away halfway.
    const stored = intent.nightlight || null;
    const nl = stored && stored.on ? stored : null;
    const curFade = stored && stored.on === false ? 'stop' : !nl ? '' : (nl.tbri || 0) > 0 ? 'in' : 'out';
    const fade = h('select', { class: 'select' });
    [['', 'No slow fade'], ['out', 'Fade out, then off'], ['in', 'Wake-up: fade in from dim'], ['stop', 'Stop a fade that is running']].forEach(([v, t]) => fade.append(h('option', { value: v, text: t, selected: curFade === v })));
    const fadeMin = h('input', { class: 'input', type: 'number', min: 1, max: 255, value: nl ? (nl.dur || nl.minutes || 30) : 30, placeholder: 'minutes' });
    const fadeWrap = h('div', { class: 'row' }, fade, h('div', { style: { width: '90px', flex: 'none' } }, fadeMin), h('span', { class: 'small muted', text: 'min' }));
    root.append(UI.field('Power', power), UI.field('Brightness %', bri),
      UI.field('Colour', h('label', { class: 'check' }, useCol, col, h('span', { class: 'small muted', text: 'set a colour' }))),
      UI.field('Preset (by name)', preset), UI.field('Effect', effect), UI.field('Palette', palette), UI.field('Fade time (seconds)', trans),
      h('div', { class: 'full' }, UI.field('Slow fade', fadeWrap, 'Fade out over a while for bedtime, or fade in slowly to wake up.')));
    // Name suggestions from every light; typing any name still works.
    catalog('all', 60000).then((cat) => {
      const list = (id, names) => { let dl = document.getElementById(id); if (!dl) { dl = h('datalist', { id }); document.body.append(dl); } dl.innerHTML = ''; names.forEach((n) => dl.append(h('option', { value: n }))); };
      list('jl-preset-names', cat.presets.map((p) => p.name)); list('jl-effect-names', cat.effects.map((e) => e.name)); list('jl-palette-names', cat.palettes.map((p) => p.name));
    }).catch(() => {});
    // Only what the person changed is rewritten; everything else is kept
    // exactly as saved – colours 2 and 3, a sunrise-mode fade, an exact
    // brightness that the percentage box can only show rounded, …
    const touched = new Set();
    const watch = (key, ...els) => els.forEach((e) => ['input', 'change'].forEach((ev) => e.addEventListener(ev, () => touched.add(key))));
    watch('on', power); watch('bri', bri); watch('color', useCol, col); watch('preset', preset); watch('effect', effect);
    watch('palette', palette); watch('transition', trans); watch('fade', fade); watch('fadeMin', fadeMin);
    const isWake = (n) => !!(n && n.on && ((n.tbri ?? n.target) || 0) > 0);
    root.getValue = () => {
      const out = JSON.parse(JSON.stringify(intent));
      if (touched.has('on')) { delete out.on; if (power.value) out.on = power.value === 't' ? 't' : power.value === 'on'; }
      if (touched.has('bri')) { delete out.bri; delete out.brightness; if (bri.value) out.brightness = Math.max(1, Math.min(100, +bri.value)); }
      if (touched.has('color')) {
        delete out.color;
        const rest = (intent.colors || []).slice(1);
        if (useCol.checked) out.colors = [Color.hexToRgb(col.value), ...rest];
        else if (rest.some(Boolean)) out.colors = [null, ...rest];
        else delete out.colors;
      }
      for (const [key, input] of [['preset', preset], ['effect', effect], ['palette', palette]]) {
        if (touched.has(key)) { delete out[key]; if (input.value.trim()) out[key] = input.value.trim(); }
      }
      if (touched.has('transition')) { delete out.transition; if (trans.value !== '') out.transition = +trans.value; }
      const minutes = Math.max(1, Math.min(255, +fadeMin.value || 30));
      const wakeTo = () => (bri.value ? Math.round(Math.max(1, Math.min(100, +bri.value)) * 2.55) : 255);
      // Picking the same kind of fade again changes nothing (a stored
      // sunrise-mode fade stays one).
      const fadeChanged = touched.has('fade') && fade.value !== curFade;
      if (fadeChanged) {
        delete out.nightlight;
        if (isWake(intent.nightlight) && fade.value !== 'in') {
          // No longer a wake-up: its "start dim" goes, and the brightness
          // box (which showed where the wake-up ended) is the brightness.
          delete out.bri; delete out.brightness;
          if (bri.value) out.brightness = Math.max(1, Math.min(100, +bri.value));
        }
        if (fade.value === 'out') out.nightlight = { on: true, minutes, mode: 'fade', target: 0 };
        else if (fade.value === 'stop') out.nightlight = { on: false };
        else if (fade.value === 'in') {
          // Start dim and climb to the chosen brightness (or full).
          out.nightlight = { on: true, minutes, mode: 'fade', target: wakeTo() };
          out.on = true; delete out.brightness; out.bri = 1;
        }
      } else {
        if (touched.has('fadeMin') && out.nightlight && out.nightlight.on) {
          // Only the length changed: keep the fade's mode and end point.
          out.nightlight = { ...out.nightlight, dur: minutes };
          delete out.nightlight.minutes;
        }
        if (touched.has('bri') && isWake(out.nightlight)) {
          // The brightness box shows where a wake-up fade ends.
          out.nightlight = { ...out.nightlight, tbri: wakeTo() };
          delete out.nightlight.target; delete out.brightness; out.bri = 1;
        }
      }
      if (!Object.keys(out).some((k) => k !== 'transition' && k !== 'best_effort')) throw new Error('Say what the lights should do: power, brightness, a colour, a preset or an effect');
      return out;
    };
    return root;
  }

  function describeIntent(intent) {
    if (!intent) return '';
    const parts = [];
    if (intent.on === false) parts.push('off');
    else if (intent.on === 't') parts.push('toggle');
    else if (intent.on === true) parts.push('on');
    if (intent.preset) parts.push(`preset “${intent.preset}”`);
    if (intent.colors && intent.colors[0]) parts.push(Color.rgbToHex(intent.colors[0]));
    if (intent.effect) parts.push(intent.effect);
    if (intent.palette) parts.push(intent.palette);
    if (intent.bri && !(intent.nightlight && intent.nightlight.on && intent.nightlight.tbri)) parts.push(Math.round(intent.bri / 2.55) + '%');
    if (intent.kelvin) parts.push(`white ${intent.kelvin} K`);
    if (intent.nightlight && intent.nightlight.on) parts.push(`${(intent.nightlight.tbri || 0) > 0 ? 'fade in' : 'fade out'} over ${intent.nightlight.dur || 60} min`);
    else if (intent.nightlight && intent.nightlight.on === false) parts.push('stop fading');
    return parts.join(', ') || 'change';
  }

  window.Lights = { open, apply, tileBody, catalog, targetKey, targetValue, targetLabel, devicesFor, targetSelect, intentEditor, describeIntent, copyLookDialog, copyPresetDialog, copyFromDialog, saveSceneDialog, addFavourite, chooseDevicesDialog, clearCache: () => catalogCache.clear() };
})();
