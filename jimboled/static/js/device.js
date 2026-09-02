/* WLED device tiles and the full control panel. */
(function () {
  const { h, el, esc } = UI;

  function isOn(d) { return !!(d.state && d.state.on); }
  function mainColors(d) { const m = d.state && d.state.main; return (m && m.col) || [[0, 0, 0], [0, 0, 0], [0, 0, 0]]; }
  function signalIcon(d) { const s = d.info && d.info.signal; if (!d.online) return 'wifiOff'; return 'wifi'; }

  async function send(d, state, opts) {
    try {
      const r = await api.post(`/api/devices/${d.id}/state`, { state });
      if (window.App) App.patchDevice(r.device);
      return r.device;
    } catch (err) { UI.notifyError(err); if (window.App) App.refresh(); return null; }
  }
  const sendThrottled = UI.throttle((d, state) => send(d, state), 150);

  // ------------------------------------------------------------ tile
  /** Returns element with .update(d). tileSize: s|m|l|xl */
  function tileBody(d, size) {
    const root = h('div', { class: 'tile-body' });
    const power = UI.toggle(isOn(d), (on) => send(d, { on }), 'lg');
    const bri = UI.slider({ icon: 'sun', min: 1, max: 255, value: (d.state && d.state.bri) || 128, format: (v) => Math.round(v / 2.55) + '%', onInput: (v) => sendThrottled(d, { bri: v, on: true }), onChange: (v) => send(d, { bri: v, on: true }) });
    const swatches = h('div', { class: 'swatches' });
    const effectLine = h('div', { class: 'row small muted', style: { gap: '6px' } });
    const presets = h('div', { class: 'chips' });
    const top = h('div', { class: 'row' }, power, h('div', { class: 'grow' }, bri));
    root.append(top);
    if (size !== 's') root.append(h('div', { class: 'row between' }, swatches, effectLine));
    if (size === 'l' || size === 'xl') root.append(presets);
    root.update = (dd) => {
      d = dd;
      power.setOn(isOn(d)); power.disabled = !d.online;
      bri.setValue((d.state && d.state.bri) || 0); bri.input.disabled = !d.online;
      swatches.innerHTML = '';
      mainColors(d).slice(0, 3).forEach((c, i) => {
        const s = h('button', { class: 'swatch', style: { background: Color.css(c) }, title: `Colour ${i + 1}` });
        s.onclick = (e) => { e.stopPropagation(); openPanel(d.id, 'control'); };
        swatches.append(s);
      });
      const fx = d.effect_name ? `${d.effect_name}` : '';
      const pal = d.palette_name && d.palette_name !== 'Default' ? ` · ${d.palette_name}` : '';
      effectLine.innerHTML = d.online ? `${icon('wand')}<span class="ellipsis">${esc(fx)}${esc(pal)}</span>` : `<span class="muted">${esc(d.last_error || 'offline')}</span>`;
      if (root.contains(presets)) {
        presets.innerHTML = '';
        const list = (d._presets || []).slice(0, 6);
        for (const p of list) {
          const c = h('button', { class: 'chip' + (d.state && d.state.ps === p.id ? ' active' : ''), text: p.name });
          c.onclick = (e) => { e.stopPropagation(); send(d, { ps: p.id }); };
          presets.append(c);
        }
        if (!list.length && d.online) presets.append(h('span', { class: 'small faint', text: 'No presets saved on this controller yet' }));
      }
    };
    root.update(d);
    return root;
  }

  // ---------------------------------------------------------- panel
  const panels = {};
  async function openPanel(deviceId, tab) {
    if (panels[deviceId]) { panels[deviceId].setTab(tab || 'control'); return panels[deviceId]; }
    let d;
    try { d = (await api.get(`/api/devices/${deviceId}`)).device; } catch (e) { return UI.notifyError(e); }
    const body = h('div', { class: 'stack' });
    const content = h('div', { class: 'stack' });
    const tabsEl = UI.tabs([{ id: 'control', label: 'Control' }, { id: 'presets', label: 'Presets' }, { id: 'segments', label: 'Segments' }, { id: 'settings', label: 'Device' }], tab || 'control', (id) => setTab(id));
    body.append(tabsEl, content);
    const menuBtn = el(`<button class="btn ghost icon" aria-label="More">${icon('more')}</button>`);
    menuBtn.onclick = () => UI.menu(menuBtn, [
      { label: 'Refresh now', icon: 'refresh', onClick: () => reload(true) },
      { label: 'Open WLED\'s own page', icon: 'externalLink', onClick: () => window.open(`http://${d.host}`, '_blank') },
      { label: 'Reload effect list', icon: 'list', onClick: async () => { try { d = (await api.post(`/api/devices/${d.id}/catalog`)).device; render(); UI.toast('Effects reloaded', 'success'); } catch (e) { UI.notifyError(e); } } },
      '-',
      { label: 'Reboot controller', icon: 'rotate', onClick: async () => { if (await UI.confirm({ title: 'Reboot', message: `Reboot ${d.name}? Lights go off for a few seconds.`, okText: 'Reboot' })) { try { await api.post(`/api/devices/${d.id}/reboot`); UI.toast('Rebooting…', 'success'); } catch (e) { UI.notifyError(e); } } } },
      { label: 'Remove from dashboard', icon: 'trash', danger: true, onClick: () => removeDevice(d, () => m.close()) },
    ]);
    const m = UI.modal({ title: d.name, icon: d.icon || 'bulb', body, full: true, headActions: menuBtn, onClose: () => { delete panels[deviceId]; clearInterval(poll); } });
    let current = tab || 'control';
    let refreshing = false;
    async function reload(force) {
      if (refreshing) return; refreshing = true;
      try { d = (await (force ? api.post(`/api/devices/${d.id}/refresh`) : api.get(`/api/devices/${d.id}`))).device; m.setTitle(d.name); if (current !== 'control' || !document.activeElement || document.activeElement.tagName !== 'INPUT') render(false); }
      catch (e) { /* offline blip */ } finally { refreshing = false; }
    }
    const poll = setInterval(() => reload(false), 4000);
    function setTab(id) { current = id; render(); }
    function render(full) {
      content.innerHTML = '';
      if (!d.online && current !== 'settings') { content.append(el(`<div class="alert warn">${icon('wifiOff')} <b>${esc(d.name)}</b> is offline: ${esc(d.last_error || 'not reachable')}. Controls will work again when it reconnects.</div>`)); }
      if (current === 'control') content.append(controlTab(d));
      else if (current === 'presets') content.append(presetsTab(d));
      else if (current === 'segments') content.append(segmentsTab(d));
      else content.append(settingsTab(d, m));
    }
    render();
    const api_ = { setTab, close: m.close, reloadDevice: (dd) => { d = dd; } };
    panels[deviceId] = api_;
    return api_;
  }

  function segById(d, id) { const segs = (d.state_full && d.state_full.seg) || []; return segs.find((s) => s.id === id) || segs[0] || {}; }
  function caps(d) { const lc = (d.info_full && d.info_full.leds && (d.info_full.leds.lc || 0)) || 1; return { rgb: !!(lc & 1), white: !!(lc & 2), cct: !!(lc & 4) }; }

  // ---- Control tab
  function controlTab(d) {
    const root = h('div', { class: 'stack' });
    const segs = (d.state_full && d.state_full.seg) || [];
    let segId = d._segId ?? (d.state_full ? d.state_full.mainseg : 0);
    if (!segs.some((s) => s.id === segId)) segId = segs.length ? segs[0].id : 0;
    const seg = segById(d, segId);
    const st = d.state_full || {};
    const c = caps(d);
    const target = () => ({ id: segId });

    // master row
    const power = UI.toggle(!!st.on, (on) => send(d, { on }), 'lg');
    const bri = UI.slider({ label: 'Brightness', min: 1, max: 255, value: st.bri || 128, format: (v) => Math.round(v / 2.55) + '%', onInput: (v) => sendThrottled(d, { bri: v, on: true }), onChange: (v) => send(d, { bri: v, on: true }) });
    root.append(h('div', { class: 'card' }, h('div', { class: 'row' }, power, h('div', { class: 'grow' }, bri))));

    // segment selector when more than one
    if (segs.length > 1) {
      const chips = h('div', { class: 'chips' });
      for (const s of segs) {
        const ch = h('button', { class: 'chip' + (s.id === segId ? ' active' : ''), text: s.n || `Segment ${s.id}` });
        ch.onclick = () => { d._segId = s.id; root.replaceWith(controlTab(d)); };
        chips.append(ch);
      }
      root.append(h('div', { class: 'card' }, h('div', { class: 'row between' }, h('span', { class: 'small muted', text: 'Controlling segment' }), chips)));
    }

    // effect metadata
    const effects = d.effects || [];
    const fx = effects.find((e) => e.id === seg.fx) || { sliders: [], colors: [{ visible: true, label: 'Colour 1' }, { visible: true, label: 'Colour 2' }, { visible: true, label: 'Colour 3' }], palette: { visible: true } };

    // colours
    const colorCard = h('div', { class: 'card' });
    colorCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Colours' })));
    const swRow = h('div', { class: 'row wrap' });
    const cols = seg.col || [[255, 160, 0], [0, 0, 0], [0, 0, 0]];
    let anyColor = false;
    (fx.colors || []).forEach((meta, i) => {
      if (!meta.visible) return; anyColor = true;
      const wrap = h('div', { class: 'stack', style: { alignItems: 'center', gap: '4px' } });
      const sw = h('button', { class: 'swatch lg', style: { background: Color.css(cols[i]) }, title: meta.label });
      sw.onclick = () => openColor(d, segId, i, cols[i], c, (rgbw, cct) => { sw.style.background = Color.css(rgbw); });
      wrap.append(sw, h('span', { class: 'small muted', text: meta.label }));
      swRow.append(wrap);
    });
    if (c.cct) {
      const cct = UI.slider({ label: 'White temperature', min: 0, max: 255, value: seg.cct ?? 127, fill: 'linear-gradient(90deg,#ffb46b,#fff,#bcd7ff)', format: (v) => (v < 85 ? 'warm' : v > 170 ? 'cool' : 'neutral'), onChange: (v) => send(d, { seg: { ...target(), cct: v } }) });
      colorCard.append(swRow, cct);
    } else colorCard.append(swRow);
    if (anyColor || c.cct) root.append(colorCard);

    // effect + palette
    const fxCard = h('div', { class: 'card' });
    const fxBtn = el(`<button class="btn">${icon('wand')}<span class="ellipsis">${esc(fx.name || 'Solid')}</span>${icon('chevronRight')}</button>`);
    fxBtn.onclick = () => pickEffect(d, segId, seg.fx);
    const palBtn = el(`<button class="btn">${icon('palette')}<span class="ellipsis">${esc((d.palettes || []).find((p) => p.id === seg.pal)?.name || 'Default')}</span>${icon('chevronRight')}</button>`);
    palBtn.onclick = () => pickPalette(d, segId, seg.pal);
    const flags = fx.flags || {};
    const badges = h('div', { class: 'row', style: { gap: '4px' } });
    if (flags.two_d) badges.append(el(`<span class="badge">2D</span>`));
    if (flags.audio_volume || flags.audio_frequency) badges.append(el(`<span class="badge">audio</span>`));
    fxCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Effect' }), badges), h('div', { class: 'row wrap' }, fxBtn, (fx.palette && fx.palette.visible !== false) ? palBtn : null));
    for (const s of fx.sliders || []) {
      if (!s.visible) continue;
      if (s.type === 'check') {
        const t = UI.toggle(!!seg[s.key], (v) => send(d, { seg: { ...target(), [s.key]: v } }));
        fxCard.append(h('div', { class: 'row between' }, h('span', { class: 'small', text: s.label }), t));
      } else {
        fxCard.append(UI.slider({ label: s.label, min: 0, max: s.max || 255, value: seg[s.key] ?? 128, onInput: (v) => sendThrottled(d, { seg: { ...target(), [s.key]: v } }), onChange: (v) => send(d, { seg: { ...target(), [s.key]: v } }) }));
      }
    }
    root.append(fxCard);

    // segment quick toggles
    const segCard = h('div', { class: 'card' });
    segCard.append(h('div', { class: 'card-title' }, h('h3', { text: segs.length > 1 ? (seg.n || `Segment ${seg.id}`) : 'Strip options' })));
    const segBri = UI.slider({ label: 'Segment brightness', min: 0, max: 255, value: seg.bri ?? 255, format: (v) => Math.round(v / 2.55) + '%', onChange: (v) => send(d, { seg: { ...target(), bri: v } }) });
    const rowT = h('div', { class: 'row wrap', style: { gap: '18px' } });
    const mk = (label, key) => h('label', { class: 'row', style: { gap: '8px' } }, UI.toggle(!!seg[key], (v) => send(d, { seg: { ...target(), [key]: v } })), h('span', { class: 'small', text: label }));
    rowT.append(mk('Segment on', 'on'), mk('Reverse', 'rev'), mk('Mirror', 'mi'), mk('Freeze', 'frz'));
    segCard.append(segBri, rowT);
    const trans = UI.slider({ label: 'Transition time', min: 0, max: 50, value: st.transition ?? 7, format: (v) => (v / 10).toFixed(1) + ' s', onChange: (v) => send(d, { transition: v }) });
    segCard.append(trans);
    root.append(segCard);
    return root;
  }

  function openColor(d, segId, index, current, c, onLive) {
    let last = null;
    const picker = Color.picker({ rgb: current || [255, 160, 0], hasWhite: c.white, hasCct: false, onChange: (rgbw) => { last = rgbw; onLive && onLive(rgbw); const col = []; col[index] = c.white ? rgbw : rgbw.slice(0, 3); sendThrottled(d, { seg: { id: segId, col: fillCols(col, index) } }); } });
    const done = el(`<button class="btn primary">Done</button>`);
    const m = UI.modal({ title: `Colour ${index + 1}`, icon: 'palette', body: picker, footer: [done] });
    done.onclick = () => { if (last) { const col = []; col[index] = c.white ? last : last.slice(0, 3); send(d, { seg: { id: segId, col: fillCols(col, index) } }); } m.close(); };
  }
  /** WLED accepts [] entries meaning "leave this slot alone". */
  function fillCols(col, index) { const out = []; for (let i = 0; i <= index; i++) out.push(i === index ? col[index] : []); return out; }

  function pickEffect(d, segId, currentId) {
    const list = d.effects || [];
    const body = h('div', { class: 'stack' });
    const search = h('input', { class: 'input', placeholder: 'Search effects…' });
    const filters = h('div', { class: 'chips' });
    let filter = 'all';
    const grid = h('div', { class: 'option-grid' });
    const fBtn = (id, label) => { const b = h('button', { class: 'chip' + (filter === id ? ' active' : ''), text: label }); b.onclick = () => { filter = id; filters.querySelectorAll('.chip').forEach((x) => x.classList.remove('active')); b.classList.add('active'); draw(); }; return b; };
    filters.append(fBtn('all', 'All'), fBtn('1d', 'Strip'), fBtn('2d', '2D'), fBtn('audio', 'Sound reactive'));
    function draw() {
      grid.innerHTML = '';
      const q = search.value.trim().toLowerCase();
      for (const e of list) {
        if (q && !e.name.toLowerCase().includes(q)) continue;
        const f = e.flags || {};
        if (filter === '1d' && !(f.one_d || f.zero_d)) continue;
        if (filter === '2d' && !f.two_d) continue;
        if (filter === 'audio' && !(f.audio_volume || f.audio_frequency)) continue;
        const o = h('button', { class: 'option' + (e.id === currentId ? ' active' : '') });
        const meta = h('div', { class: 'meta' });
        if (f.two_d) meta.append(h('span', { text: '2D' }));
        if (f.audio_volume || f.audio_frequency) meta.append(h('span', { text: '♪' }));
        o.append(h('span', { class: 'name', text: e.name }), meta);
        o.onclick = async () => { const dev = await send(d, { seg: { id: segId, fx: e.id, fxdef: true } }); m.close(); if (dev) refreshPanel(d.id, 'control'); };
        grid.append(o);
      }
      if (!grid.children.length) grid.append(h('div', { class: 'muted small', text: 'No effects match' }));
    }
    search.oninput = UI.debounce(draw, 80);
    body.append(search, filters, grid);
    const m = UI.modal({ title: 'Choose an effect', icon: 'wand', body, wide: true });
    draw();
  }
  function pickPalette(d, segId, currentId) {
    const body = h('div', { class: 'stack' });
    const search = h('input', { class: 'input', placeholder: 'Search palettes…' });
    const grid = h('div', { class: 'option-grid' });
    function draw() {
      grid.innerHTML = '';
      const q = search.value.trim().toLowerCase();
      for (const p of d.palettes || []) {
        if (q && !p.name.toLowerCase().includes(q)) continue;
        const o = h('button', { class: 'option' + (p.id === currentId ? ' active' : '') }, h('span', { class: 'name', text: p.name }));
        o.onclick = async () => { const dev = await send(d, { seg: { id: segId, pal: p.id } }); m.close(); if (dev) refreshPanel(d.id, 'control'); };
        grid.append(o);
      }
    }
    search.oninput = UI.debounce(draw, 80);
    body.append(search, grid);
    const m = UI.modal({ title: 'Choose a palette', icon: 'palette', body, wide: true });
    draw();
  }
  async function refreshPanel(id, tab) { const p = panels[id]; if (!p) return; try { p.reloadDevice((await api.get(`/api/devices/${id}`)).device); p.setTab(tab); } catch (e) {} }

  // ---- Presets tab
  function presetsTab(d) {
    const root = h('div', { class: 'stack' });
    const st = d.state_full || {};
    const grid = h('div', { class: 'option-grid', style: { maxHeight: 'none' } });
    const list = d.presets || [];
    for (const p of list) {
      const o = h('button', { class: 'option' + (st.ps === p.id ? ' active' : '') });
      const meta = h('div', { class: 'meta' }, h('span', { text: `#${p.id}` }));
      if (p.is_playlist) meta.append(h('span', { text: 'playlist' }));
      if (p.quick_label) meta.append(h('span', { text: p.quick_label }));
      o.append(h('span', { class: 'name', text: p.name }), meta);
      o.onclick = async () => { const dev = await send(d, { ps: p.id }); if (dev) refreshPanel(d.id, 'presets'); };
      o.oncontextmenu = (e) => { e.preventDefault(); presetMenu(o, d, p); };
      const more = el(`<button class="btn ghost icon sm" style="position:absolute;top:4px;right:4px" aria-label="Preset options">${icon('more')}</button>`);
      more.onclick = (e) => { e.stopPropagation(); presetMenu(more, d, p); };
      o.style.position = 'relative'; o.append(more);
      grid.append(o);
    }
    const saveBtn = el(`<button class="btn primary">${icon('save')} Save current look as preset</button>`);
    saveBtn.onclick = () => savePresetDialog(d);
    const stopPl = el(`<button class="btn">${icon('stop')} Stop playlist</button>`);
    stopPl.onclick = async () => { await send(d, { playlist: {} }); refreshPanel(d.id, 'presets'); };
    const plBtn = el(`<button class="btn">${icon('play')} New playlist</button>`);
    plBtn.onclick = () => playlistDialog(d);
    root.append(h('div', { class: 'row wrap' }, saveBtn, plBtn, (st.pl != null && st.pl !== -1) ? stopPl : null));
    if (list.length) root.append(grid);
    else root.append(el(`<div class="empty">${icon('star')}<h3>No presets yet</h3><p>Set up a look you like on the Control tab, then save it here. Presets are stored on the controller itself, so they also work from the WLED app.</p></div>`));
    return root;
  }
  function presetMenu(anchor, d, p) {
    UI.menu(anchor, [
      { label: 'Apply', icon: 'play', onClick: () => send(d, { ps: p.id }).then(() => refreshPanel(d.id, 'presets')) },
      { label: 'Overwrite with current look', icon: 'save', onClick: async () => { if (await UI.confirm({ title: 'Overwrite preset', message: `Replace "${p.name}" with the current look?`, okText: 'Overwrite' })) { try { await api.post(`/api/devices/${d.id}/presets`, { slot: p.id, name: p.name, quick_label: p.quick_label }); UI.toast('Preset updated', 'success'); refreshPanel(d.id, 'presets'); } catch (e) { UI.notifyError(e); } } } },
      { label: 'Rename', icon: 'edit', onClick: async () => { const name = await UI.prompt({ title: 'Rename preset', label: 'Name', value: p.name, maxlength: 32 }); if (name && name !== p.name) { try { await api.post(`/api/devices/${d.id}/presets`, { slot: p.id, name, quick_label: p.quick_label }); refreshPanel(d.id, 'presets'); } catch (e) { UI.notifyError(e); } } } },
      '-',
      { label: 'Delete', icon: 'trash', danger: true, onClick: async () => { if (await UI.confirm({ title: 'Delete preset', message: `Delete "${p.name}" from ${d.name}?`, okText: 'Delete', danger: true })) { try { await api.del(`/api/devices/${d.id}/presets/${p.id}`); UI.toast('Preset deleted', 'success'); refreshPanel(d.id, 'presets'); } catch (e) { UI.notifyError(e); } } } },
    ]);
  }
  function savePresetDialog(d) {
    const name = h('input', { class: 'input', placeholder: 'e.g. Movie night', maxlength: 32 });
    const ql = h('input', { class: 'input', placeholder: 'e.g. MV', maxlength: 2, style: { width: '90px' } });
    const ib = h('input', { type: 'checkbox', checked: true });
    const sb = h('input', { type: 'checkbox', checked: true });
    const body = h('div', { class: 'stack' }, UI.field('Preset name', name), UI.field('Quick label (optional, 2 letters)', ql, 'Shown as a shortcut in the WLED app'), h('label', { class: 'check' }, ib, h('span', { text: 'Include brightness' })), h('label', { class: 'check' }, sb, h('span', { text: 'Include segment layout' })));
    const save = el(`<button class="btn primary" data-busy="Saving…">Save preset</button>`);
    const m = UI.modal({ title: 'Save preset', icon: 'save', body, footer: [save] });
    save.onclick = () => UI.busy(save, (async () => { if (!name.value.trim()) throw new Error('Give the preset a name'); await api.post(`/api/devices/${d.id}/presets`, { name: name.value.trim(), quick_label: ql.value.trim(), include_brightness: ib.checked, save_segment_bounds: sb.checked }); UI.toast('Preset saved', 'success'); m.close(); refreshPanel(d.id, 'presets'); })().catch(UI.notifyError));
  }
  function playlistDialog(d) {
    const presets = (d.presets || []).filter((p) => !p.is_playlist);
    if (!presets.length) return UI.toast('Save a few presets first, then build a playlist from them', 'error');
    const name = h('input', { class: 'input', placeholder: 'e.g. Evening rotation', maxlength: 32 });
    const entries = [];
    const list = h('div', { class: 'list' });
    const add = () => { entries.push({ ps: presets[0].id, dur: 30, tr: 0.7 }); draw(); };
    function draw() {
      list.innerHTML = '';
      entries.forEach((e, i) => {
        const sel = h('select', { class: 'select', style: { flex: '2' } });
        for (const p of presets) sel.append(h('option', { value: p.id, text: p.name, selected: p.id === e.ps }));
        sel.onchange = () => { e.ps = +sel.value; };
        const dur = h('input', { class: 'input', type: 'number', min: 1, max: 6553, value: e.dur, style: { width: '84px' }, title: 'seconds' });
        dur.onchange = () => { e.dur = +dur.value; };
        const rm = el(`<button class="btn ghost icon sm">${icon('x')}</button>`); rm.onclick = () => { entries.splice(i, 1); draw(); };
        list.append(h('div', { class: 'list-item' }, sel, h('span', { class: 'small muted', text: 'for' }), dur, h('span', { class: 'small muted', text: 's' }), rm));
      });
    }
    const addBtn = el(`<button class="btn sm">${icon('plus')} Add step</button>`); addBtn.onclick = add;
    const repeat = h('input', { class: 'input', type: 'number', min: 0, max: 255, value: 0, style: { width: '90px' } });
    const shuffle = h('input', { type: 'checkbox' });
    const body = h('div', { class: 'stack' }, UI.field('Playlist name', name), h('h4', { text: 'Steps' }), list, addBtn, h('div', { class: 'row wrap', style: { gap: '18px' } }, UI.field('Repeat (0 = forever)', repeat), h('label', { class: 'check' }, shuffle, h('span', { text: 'Shuffle' }))));
    add(); add();
    const save = el(`<button class="btn primary" data-busy="Saving…">Save & start</button>`);
    const m = UI.modal({ title: 'New playlist', icon: 'play', body, footer: [save], wide: true });
    save.onclick = () => UI.busy(save, (async () => {
      if (!name.value.trim()) throw new Error('Give the playlist a name');
      if (!entries.length) throw new Error('Add at least one step');
      const playlist = { ps: entries.map((e) => e.ps), dur: entries.map((e) => Math.round(e.dur * 10)), transition: entries.map(() => 7), repeat: +repeat.value, end: 0, r: shuffle.checked };
      await api.post(`/api/devices/${d.id}/presets`, { name: name.value.trim(), state: { on: true, playlist } });
      await send(d, { playlist });
      UI.toast('Playlist saved and started', 'success'); m.close(); refreshPanel(d.id, 'presets');
    })().catch(UI.notifyError));
  }

  // ---- Segments tab
  function segmentsTab(d) {
    const root = h('div', { class: 'stack' });
    const segs = (d.state_full && d.state_full.seg) || [];
    const total = (d.info_full && d.info_full.leds && d.info_full.leds.count) || 0;
    root.append(el(`<p class="muted small">Segments split the strip into independent zones (${total} LEDs total). Each segment has its own colours and effect. Segment 0 always exists.</p>`));
    const list = h('div', { class: 'list' });
    for (const s of segs) {
      const item = h('div', { class: 'list-item' });
      const dot = h('div', { class: 'swatch', style: { background: Color.css((s.col || [])[0]) } });
      const info = h('div', { class: 'grow' }, h('span', { class: 'title', text: s.n || `Segment ${s.id}` }), h('span', { class: 'sub', text: `LEDs ${s.start}–${s.stop - 1} · ${(d.effects || []).find((e) => e.id === s.fx)?.name || 'Solid'}${s.rev ? ' · reversed' : ''}${s.mi ? ' · mirrored' : ''}` }));
      const on = UI.toggle(!!s.on, (v) => send(d, { seg: { id: s.id, on: v } }));
      const edit = el(`<button class="btn ghost icon sm">${icon('edit')}</button>`); edit.onclick = () => segmentDialog(d, s, total);
      item.append(dot, info, on, edit);
      list.append(item);
    }
    const addBtn = el(`<button class="btn">${icon('plus')} Add segment</button>`);
    addBtn.onclick = () => segmentDialog(d, null, total);
    root.append(list, h('div', { class: 'row' }, addBtn));
    return root;
  }
  function segmentDialog(d, s, total) {
    const segs = (d.state_full && d.state_full.seg) || [];
    const nextId = segs.length ? Math.max(...segs.map((x) => x.id)) + 1 : 0;
    const last = segs[segs.length - 1];
    const seg = s || { id: nextId, start: last ? last.stop : 0, stop: total, n: '', grp: 1, spc: 0, of: 0, rev: false, mi: false };
    const name = h('input', { class: 'input', value: seg.n || '', maxlength: 32, placeholder: `Segment ${seg.id}` });
    const start = h('input', { class: 'input', type: 'number', min: 0, max: total - 1, value: seg.start });
    const stop = h('input', { class: 'input', type: 'number', min: 1, max: total, value: seg.stop });
    const grp = h('input', { class: 'input', type: 'number', min: 1, max: 255, value: seg.grp || 1 });
    const spc = h('input', { class: 'input', type: 'number', min: 0, max: 255, value: seg.spc || 0 });
    const of = h('input', { class: 'input', type: 'number', min: -255, max: 255, value: seg.of || 0 });
    const rev = h('input', { type: 'checkbox', checked: !!seg.rev });
    const mi = h('input', { type: 'checkbox', checked: !!seg.mi });
    const body = h('div', { class: 'form-grid' }, h('div', { class: 'full' }, UI.field('Name', name)), UI.field('First LED', start), UI.field('Last LED (exclusive)', stop, `1 – ${total}`), UI.field('Grouping', grp, 'LEDs treated as one pixel'), UI.field('Spacing', spc, 'LEDs skipped between groups'), UI.field('Offset', of), h('div', { class: 'row wrap full', style: { gap: '18px' } }, h('label', { class: 'check' }, rev, h('span', { text: 'Reverse direction' })), h('label', { class: 'check' }, mi, h('span', { text: 'Mirror' }))));
    const save = el(`<button class="btn primary" data-busy="Saving…">${s ? 'Save' : 'Create'}</button>`);
    const footer = [save];
    if (s && s.id !== 0) { const del = el(`<button class="btn danger">Delete</button>`); del.onclick = async () => { if (await UI.confirm({ title: 'Delete segment', message: `Delete "${s.n || 'Segment ' + s.id}"?`, okText: 'Delete', danger: true })) { await send(d, { seg: { id: s.id, stop: 0 } }); m.close(); refreshPanel(d.id, 'segments'); } }; footer.unshift(del); }
    const m = UI.modal({ title: s ? 'Edit segment' : 'New segment', icon: 'layers', body, footer });
    save.onclick = () => UI.busy(save, (async () => {
      const payload = { id: seg.id, start: +start.value, stop: +stop.value, n: name.value.trim(), grp: +grp.value, spc: +spc.value, of: +of.value, rev: rev.checked, mi: mi.checked };
      if (payload.stop <= payload.start) throw new Error('Last LED must be after first LED');
      if (!s) payload.on = true;
      const dev = await send(d, { seg: [payload] });
      if (dev) { m.close(); refreshPanel(d.id, 'segments'); }
    })().catch(UI.notifyError));
  }

  // ---- Device tab
  function settingsTab(d, m) {
    const root = h('div', { class: 'stack' });
    const st = d.state_full || {}, info = d.info_full || {};
    // nightlight
    const nl = st.nl || {};
    const nlCard = h('div', { class: 'card' });
    const nlToggle = UI.toggle(!!nl.on, (v) => send(d, { nl: { on: v, dur: +nlDur.input.value, mode: +nlMode.value, tbri: +nlBri.input.value } }).then(() => refreshPanel(d.id, 'settings')));
    const nlDur = UI.slider({ label: 'Duration', min: 1, max: 255, value: nl.dur || 60, format: (v) => v + ' min' });
    const nlBri = UI.slider({ label: 'Target brightness', min: 0, max: 255, value: nl.tbri || 0, format: (v) => Math.round(v / 2.55) + '%' });
    const nlMode = h('select', { class: 'select' });
    [['1', 'Fade to target'], ['0', 'Switch at the end'], ['2', 'Fade to second colour'], ['3', 'Sunrise']].forEach(([v, t]) => nlMode.append(h('option', { value: v, text: t, selected: String(nl.mode ?? 1) === v })));
    nlCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Sleep timer' }), nlToggle), el(`<p class="muted small">Gradually dims the lights and turns them off. ${nl.on && nl.rem > 0 ? `<b>Active – ${UI.fmtDuration(nl.rem)} remaining.</b>` : ''}</p>`), nlDur, nlBri, UI.field('Mode', nlMode));
    root.append(nlCard);
    // sync
    const udpn = st.udpn || {};
    const syncCard = h('div', { class: 'card' });
    syncCard.append(h('div', { class: 'card-title' }, h('h3', { text: 'Sync with other controllers' })), el(`<p class="muted small">WLED can mirror changes to other WLED devices on the network (UDP sync).</p>`), h('div', { class: 'row wrap', style: { gap: '18px' } }, h('label', { class: 'row', style: { gap: '8px' } }, UI.toggle(!!udpn.send, (v) => send(d, { udpn: { send: v } })), h('span', { class: 'small', text: 'Send changes to others' })), h('label', { class: 'row', style: { gap: '8px' } }, h('span', { class: 'small muted', text: `Receive: ${udpn.recv ? 'on' : 'off'} (set in WLED settings)` }))));
    const lor = h('select', { class: 'select' });
    [['0', 'Normal'], ['1', 'Override until live data ends'], ['2', 'Override until reboot']].forEach(([v, t]) => lor.append(h('option', { value: v, text: t, selected: String(st.lor ?? 0) === v })));
    lor.onchange = () => send(d, { lor: +lor.value });
    syncCard.append(UI.field('Live data override', lor, 'Ignore incoming realtime data (Hyperion, E1.31, etc.)'));
    root.append(syncCard);
    // info
    const leds = info.leds || {}, wifi = info.wifi || {};
    const kv = h('dl', { class: 'kv' });
    const add = (k, v) => { if (v == null || v === '') return; kv.append(h('dt', { text: k }), h('dd', { text: String(v) })); };
    add('Address', d.host); add('Firmware', info.ver ? `WLED ${info.ver}${info.cn ? ' "' + info.cn + '"' : ''}` : '–'); add('Chip', info.arch); add('LEDs', leds.count); add('Frame rate', leds.fps != null ? leds.fps + ' fps' : null);
    add('Power estimate', leds.pwr ? `${leds.pwr} mA${leds.maxpwr ? ' of ' + leds.maxpwr + ' mA limit' : ''}` : null); add('Wi‑Fi signal', wifi.signal != null ? `${wifi.signal}% (${wifi.rssi} dBm, ch ${wifi.channel})` : null); add('Uptime', info.uptime != null ? UI.fmtDuration(info.uptime) : null); add('Free memory', info.freeheap != null ? UI.fmtBytes(info.freeheap) : null); add('MAC', info.mac); add('Last seen', UI.fmtAgo(d.last_seen)); add('Response time', d.latency_ms ? d.latency_ms + ' ms' : null);
    if (info.live) add('Realtime source', `${info.lm || 'live'} ${info.lip || ''}`);
    root.append(h('div', { class: 'card' }, h('div', { class: 'card-title' }, h('h3', { text: 'About this controller' })), kv));
    // edit
    const editBtn = el(`<button class="btn">${icon('edit')} Edit name, icon & address</button>`);
    editBtn.onclick = () => editDevice(d, () => refreshPanel(d.id, 'settings'));
    const openBtn = el(`<button class="btn">${icon('externalLink')} Open WLED's own page</button>`);
    openBtn.onclick = () => window.open(`http://${d.host}`, '_blank');
    const rmBtn = el(`<button class="btn danger">${icon('trash')} Remove</button>`);
    rmBtn.onclick = () => removeDevice(d, () => m.close());
    root.append(h('div', { class: 'row wrap' }, editBtn, openBtn, rmBtn));
    return root;
  }

  function editDevice(d, onSaved) {
    const name = h('input', { class: 'input', value: d.name, maxlength: 60 });
    const host = h('input', { class: 'input mono', value: d.host, placeholder: '192.168.1.50 or wled-abc123.local' });
    let iconName = d.icon || 'bulb', color = d.color || '';
    const body = h('div', { class: 'stack' }, UI.field('Name', name), UI.field('Address', host, 'IP address or hostname of the controller'), h('h4', { text: 'Icon' }), UI.iconPicker(iconName, (n) => { iconName = n; }), h('h4', { text: 'Colour' }), UI.colorPicker(color, (c) => { color = c; }));
    const save = el(`<button class="btn primary" data-busy="Saving…">Save</button>`);
    const m = UI.modal({ title: 'Edit controller', icon: 'edit', body, footer: [save] });
    save.onclick = () => UI.busy(save, (async () => { await api.put(`/api/devices/${d.id}`, { name: name.value.trim(), host: host.value.trim(), icon: iconName, color }); UI.toast('Saved', 'success'); m.close(); onSaved && onSaved(); if (window.App) App.refresh(); })().catch(UI.notifyError));
  }
  async function removeDevice(d, onDone) {
    const ok = await UI.confirm({ title: 'Remove controller', message: `Remove "${d.name}" from the dashboard? Nothing on the controller itself is changed.`, okText: 'Remove', danger: true });
    if (!ok) return;
    try { await api.del(`/api/devices/${d.id}`); UI.toast('Removed', 'success'); onDone && onDone(); if (window.App) App.refresh(); } catch (e) { UI.notifyError(e); }
  }

  // ---- add device dialog (shared with settings & wizard)
  function addDeviceDialog(onAdded) {
    const host = h('input', { class: 'input mono', placeholder: '192.168.1.50 or wled-abc123.local', autocomplete: 'off' });
    const name = h('input', { class: 'input', placeholder: 'Optional – uses the controller\'s own name', maxlength: 60 });
    const anyway = h('input', { type: 'checkbox' });
    const probe = h('div', { class: 'small muted' });
    host.addEventListener('change', async () => { probe.textContent = 'Checking…'; try { const r = await api.post('/api/devices/probe', { host: host.value.trim() }); probe.innerHTML = `${icon('check')} Found <b>${esc(r.name)}</b> – WLED ${esc(r.ver)}, ${esc(r.led_count)} LEDs`; if (!name.value) name.placeholder = r.name; } catch (e) { probe.textContent = e.message; } });
    const body = h('div', { class: 'stack' }, UI.field('Controller address', host, 'Find it in your router\'s device list or in the WLED app. Or use "Find controllers" to search automatically.'), probe, UI.field('Name', name), h('label', { class: 'check' }, anyway, h('span', { text: 'Add even if it cannot be reached right now' })));
    const save = el(`<button class="btn primary" data-busy="Adding…">Add controller</button>`);
    const m = UI.modal({ title: 'Add a WLED controller', icon: 'plus', body, footer: [save] });
    save.onclick = () => UI.busy(save, (async () => { const r = await api.post('/api/devices', { host: host.value.trim(), name: name.value.trim(), skip_probe: anyway.checked }); UI.toast(`Added ${r.device.name}`, 'success'); m.close(); onAdded && onAdded(r.device); if (window.App) App.refresh(); })().catch(UI.notifyError));
    host.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); });
  }

  window.Device = { tileBody, openPanel, send, editDevice, removeDevice, addDeviceDialog, isOn, mainColors, signalIcon };
})();
