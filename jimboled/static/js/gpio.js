/* GPIO switch tiles, hold-to-run controller and the switch editor. */
(function () {
  const { h, el, esc } = UI;
  const MODE_LABEL = { toggle: 'On / off', momentary: 'Hold to run', pulse: 'Tap' };
  const DEFAULT_LIMIT = { toggle: 0, momentary: 60, pulse: 0 };
  const HEARTBEAT_MS = 400;

  /** Hold-to-run: press → heartbeats every 400 ms → release. Releases on any loss of contact. */
  class HoldController {
    constructor(sw, button, opts) {
      this.sw = sw; this.btn = button; this.opts = opts || {};
      this.token = null; this.timer = null; this.pointerId = null; this.startedAt = 0;
      const start = (e) => { if (e.button && e.button !== 0) return; e.preventDefault(); this.press(e); };
      button.addEventListener('pointerdown', start);
      button.addEventListener('pointerup', () => this.release('release'));
      button.addEventListener('pointercancel', () => this.release('cancel'));
      button.addEventListener('lostpointercapture', () => this.release('lost'));
      button.addEventListener('contextmenu', (e) => e.preventDefault());
      button.addEventListener('keydown', (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); this.press(); } });
      button.addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') this.release('key'); });
      button.addEventListener('blur', () => this.release('blur'));
      this._onHide = () => { if (document.hidden) this.release('hidden'); };
      this._onPageHide = () => this.release('pagehide');
      this._onWinBlur = () => this.release('winblur');
      document.addEventListener('visibilitychange', this._onHide);
      window.addEventListener('pagehide', this._onPageHide);
      window.addEventListener('blur', this._onWinBlur);
    }
    async press(e) {
      if (this.token || this.pending) return;
      if (e && e.pointerId != null) { try { this.btn.setPointerCapture(e.pointerId); } catch (_) {} this.pointerId = e.pointerId; }
      // Clear the "let go before the press landed" flag up front: a press that
      // failed used to leave it set, so the next hold released itself at once.
      this.released = false;
      this.pending = true;
      this.missed = 0;
      this.btn.classList.add('active');
      try {
        const r = await api.post(`/api/gpio/switches/${this.sw.id}/action`, { action: 'press' }, { timeout: 5000 });
        this.token = r.switch.token; this.startedAt = Date.now();
        if (this.released) { this.released = false; await this.stop('early-release'); return; }
        this.timer = setInterval(() => this.beat(), HEARTBEAT_MS);
        if (this.opts.onState) this.opts.onState(r.switch);
        if (navigator.vibrate) navigator.vibrate(10);
      } catch (err) {
        this.btn.classList.remove('active');
        UI.notifyError(err);
      } finally { this.pending = false; }
    }
    async beat() {
      if (!this.token) return;
      try {
        // A heartbeat that has not answered by the next one is already useless.
        const r = await api.post(`/api/gpio/switches/${this.sw.id}/action`, { action: 'heartbeat', token: this.token }, { timeout: 2000 });
        this.missed = 0;
        if (!r.switch.held) { this.stopLocal(); this.btn.classList.remove('active'); if (this.opts.onState) this.opts.onState(r.switch); }
        else if (this.opts.onTick) this.opts.onTick(r.switch);
      } catch (err) {
        // A 409 means an emergency stop latched mid-hold – the relay is already
        // off, so stop beating.
        if (err && err.status === 409) { this.stopLocal(); this.btn.classList.remove('active'); UI.notifyError(err); App.refresh(true); return; }
        // Otherwise keep trying: the server watchdog releases us if we really
        // are cut off. But once we have been silent for longer than the server
        // waits, that release has already happened – so stop showing the
        // switch as running. A button that says "Running…" while the relay is
        // off is worse than an error.
        this.missed = (this.missed || 0) + 1;
        const holdTimeoutMs = ((App.state.gpio && App.state.gpio.hold_timeout_s) || 1.5) * 1000;
        if (this.missed * HEARTBEAT_MS >= holdTimeoutMs) {
          this.stopLocal();
          this.btn.classList.remove('active');
          UI.toast('Lost contact with JimboLED – the switch has been released.', 'error');
        }
      }
    }
    release(reason) {
      if (this.pending && !this.token) { this.released = true; return; }
      if (!this.token) { this.btn.classList.remove('active'); return; }
      this.stop(reason);
    }
    stopLocal() { clearInterval(this.timer); this.timer = null; this.token = null; }
    async stop(reason) {
      const token = this.token; this.stopLocal(); this.btn.classList.remove('active');
      if (this.pointerId != null) { try { this.btn.releasePointerCapture(this.pointerId); } catch (_) {} this.pointerId = null; }
      try {
        // keepalive lets the release survive tab close / page hide.
        const r = await api.post(`/api/gpio/switches/${this.sw.id}/action`, { action: 'release', token: token || '' }, { keepalive: true, timeout: 10000 });
        if (this.opts.onState) this.opts.onState(r.switch);
      } catch (err) { UI.notifyError(err); }
    }
    destroy() {
      this.release('destroy');
      document.removeEventListener('visibilitychange', this._onHide);
      window.removeEventListener('pagehide', this._onPageHide);
      window.removeEventListener('blur', this._onWinBlur);
    }
  }

  function switchIcon(sw) { return sw.icon || (sw.mode === 'momentary' ? 'hand' : sw.mode === 'pulse' ? 'zap' : 'switch'); }

  async function act(sw, action, extra) {
    if (sw.confirm && (action === 'on' || action === 'toggle' || action === 'pulse') && !sw.on) {
      const ok = await UI.confirm({ title: sw.name, message: `Turn on "${sw.name}"?`, okText: 'Turn on' });
      if (!ok) return null;
    }
    try {
      const r = await api.post(`/api/gpio/switches/${sw.id}/action`, Object.assign({ action }, extra || {}));
      return r.switch;
    } catch (err) { UI.notifyError(err); return null; }
  }

  /** Render the control area of a switch tile. Returns element with .update(sw). */
  function control(sw, opts) {
    opts = opts || {};
    const root = h('div', { class: 'stack' });
    let btn, timerEl, holder;
    const status = h('div', { class: 'row between small muted' });
    timerEl = h('span', { class: 'timer' });
    const modeLabel = h('span', { text: MODE_LABEL[sw.mode] || sw.mode });
    status.append(modeLabel, timerEl);

    if (sw.mode === 'momentary') {
      btn = el(`<button class="hold-btn ${opts.big ? 'big' : ''}" type="button" aria-label="Hold to run ${esc(sw.name)}">${icon(switchIcon(sw))}<span class="label">Hold</span><span class="progress"></span></button>`);
      holder = new HoldController(sw, btn, { onState: (s) => root.update(s), onTick: (s) => root.update(s) });
      root.append(btn, status);
      root.hint = 'Press and hold the button. Let go to stop.';
    } else if (sw.mode === 'pulse') {
      btn = el(`<button class="hold-btn ${opts.big ? 'big' : ''}" type="button">${icon(switchIcon(sw))}<span class="label">Tap</span><span class="progress"></span></button>`);
      btn.onclick = async () => { btn.classList.add('active'); const s = await act(sw, 'pulse'); if (s) root.update(s); setTimeout(() => btn.classList.remove('active'), sw.pulse_ms || 500); };
      root.append(btn, status);
    } else {
      btn = el(`<button class="hold-btn ${opts.big ? 'big' : ''}" type="button">${icon('power')}<span class="label">Off</span><span class="progress"></span></button>`);
      btn.onclick = async () => { const s = await act(sw, sw.on ? 'off' : 'toggle'); if (s) root.update(s); };
      root.append(btn, status);
    }
    root.update = (s) => {
      Object.assign(sw, s);
      const label = btn.querySelector('.label');
      btn.classList.toggle('on', !!s.on);
      // A latched emergency stop is the one thing the tile refuses outright:
      // the server would reject the press anyway, so do not pretend otherwise.
      btn.disabled = !s.available || !!s.locked_out;
      if (sw.mode === 'toggle') label.textContent = s.on ? 'On' : 'Off';
      else if (sw.mode === 'momentary') label.textContent = s.on ? 'Running…' : 'Hold';
      const prog = btn.querySelector('.progress');
      if (s.on && s.max_on_seconds && s.remaining != null) { prog.style.width = (100 * (1 - s.remaining / s.max_on_seconds)) + '%'; timerEl.textContent = `${Math.ceil(s.remaining)}s left`; }
      else if (s.on) { prog.style.width = '100%'; timerEl.textContent = `on ${UI.fmtDuration(s.on_for)}`; }
      else { prog.style.width = '0'; timerEl.textContent = s.last_off_reason && /timeout|max-on/.test(s.last_off_reason) ? (s.last_off_reason === 'heartbeat-timeout' ? 'auto-released' : 'time limit reached') : ''; }
      if (!s.available) { timerEl.textContent = s.error || 'unavailable'; }
      if (s.locked_out) { label.textContent = 'Stopped'; timerEl.textContent = 'emergency stop'; }
    };
    root.update(sw);
    root.destroy = () => holder && holder.destroy();
    return root;
  }

  // ---------------------------------------------------------- editor
  // ------------------------------------------------------- switch editor
  // Written for someone who has never heard of "momentary" or "active low":
  // pick what the button is for, name it, say which wire it is on.  The
  // technical knobs are still there, folded away under "More options".
  const KINDS = {
    momentary: { icon: 'up', title: 'Move a motor', tag: 'Hold to run',
                 text: 'Runs only while a finger is on the button and stops the moment you let go. For beds, recliners, lifts, blinds and awnings.' },
    toggle: { icon: 'power', title: 'Turn something on and off', tag: 'On / off',
              text: 'Tap once for on, tap again for off. For lamps, fans, heaters and pumps.' },
    pulse: { icon: 'zap', title: 'Press for a moment', tag: 'Tap',
             text: 'Clicks the relay briefly, like pressing a button on a remote. For garage doors, gates and doorbells.' },
  };
  const DIRECTIONS = [['up', 'down'], ['open', 'close'], ['raise', 'lower'], ['in', 'out'], ['forward', 'back'], ['left', 'right'], ['extend', 'retract'], ['head up', 'head down'], ['feet up', 'feet down']];
  const DIR_ICON = { up: 'up', down: 'down', raise: 'up', lower: 'down' };
  const cap = (w) => w.charAt(0).toUpperCase() + w.slice(1);
  const slug = (t) => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);

  /** "Bed up" -> { name: "Bed down", word: "down", icon: "down" }, or null when no direction word is found. */
  function opposite(name) {
    const text = String(name || '').trim();
    for (const [a, b] of DIRECTIONS) {
      for (const [from, to] of [[a, b], [b, a]]) {
        const re = new RegExp(`(^|\\s)(${from.replace(/ /g, '\\s+')})$`, 'i');
        const m = re.exec(text);
        if (!m) continue;
        const was = m[2];
        const word = was.charAt(0) === was.charAt(0).toUpperCase() && was.charAt(0) !== was.charAt(0).toLowerCase() ? cap(to) : to;
        const last = to.split(' ').pop();
        return { name: text.slice(0, m.index) + m[1] + word, word: to, icon: DIR_ICON[last] || '' };
      }
    }
    return null;
  }

  /** A pin dropdown (best choices first) with a Test button and a fold-out pin map. */
  function pinPicker(pins, current, ownPin, isActiveHigh, label) {
    let value = current == null ? null : +current;
    const sel = h('select', { class: 'select pin-select', 'aria-label': label || 'GPIO pin' });
    const map = h('div', { class: 'pinout' });
    const bootSafe = (p) => (isActiveHigh() ? p.boot_pull === 'down' : p.boot_pull === 'up');
    function draw() {
      sel.innerHTML = '';
      sel.append(h('option', { value: '', text: 'Choose the GPIO number…', disabled: true, selected: value == null }));
      const groups = [['Best choices (stay off while the Pi starts)', []], ['Also work', []], ['Already in use', []]];
      for (const p of pins) {
        if (p.reserved) continue;
        const used = p.in_use_by && p.bcm !== ownPin;
        const text = `GPIO ${p.bcm}  ·  physical pin ${p.physical}` + (used ? `  —  used by ${p.in_use_by}` : '');
        groups[used ? 2 : (p.recommended && bootSafe(p)) ? 0 : 1][1].push(h('option', { value: String(p.bcm), text, disabled: !!used, selected: p.bcm === value }));
      }
      for (const [name, opts] of groups) if (opts.length) sel.append(h('optgroup', { label: name }, opts));
      map.innerHTML = '';
      for (const p of pins) {
        const used = p.in_use_by && p.bcm !== ownPin;
        const ok = bootSafe(p);
        const b = h('button', { type: 'button', class: 'pin' + (p.recommended ? ' rec' : '') + (p.reserved ? ' reserved' : '') + (used ? ' used' : '') + (p.bcm === value ? ' selected' : ''), title: p.note + (used ? ` (used by ${p.in_use_by})` : '') });
        b.append(h('span', { class: 'num', text: `GPIO${p.bcm}` }), h('span', { class: 'phys', text: `#${p.physical}` }), h('span', { class: 'note grow', text: used ? `in use: ${p.in_use_by}` : p.note }), h('span', { class: 'boot', title: ok ? 'Relay stays off while the Pi starts up' : 'May click briefly while the Pi starts up', text: ok ? '✓ boot-safe' : '' }));
        if (!p.reserved && !used) b.onclick = () => { value = p.bcm; draw(); };
        map.append(b);
      }
    }
    sel.onchange = () => { value = sel.value === '' ? null : +sel.value; draw(); };
    const test = el(`<button class="btn sm" type="button" title="Clicks the relay for less than half a second">${icon('zap')} Test</button>`);
    test.onclick = async () => {
      if (value == null) return UI.toast('Choose the GPIO number first', 'error');
      try { await api.post('/api/gpio/test', { pin: value, active_high: isActiveHigh(), duration_ms: 400 }); UI.toast(`Clicked GPIO ${value}. Did the right relay click?`, 'success'); } catch (e) { UI.notifyError(e); }
    };
    draw();
    const node = h('div', { class: 'stack', style: { gap: '6px' } },
      h('div', { class: 'row pin-row' }, sel, test),
      h('details', { class: 'pin-map' }, h('summary', { class: 'small muted', text: 'Show the Pi\'s pin map' }), map));
    return { el: node, redraw: draw, get value() { return value; } };
  }

  /**
   * Add or edit a switch.  ``preset`` pre-fills a new one: ``{ mode, pair,
   * name, base, active_high, interlock_group, icon, max_on_seconds, partner }``
   * where ``partner`` is a switch that should join the new one's interlock
   * group (used by "Add the matching Down button").
   */
  async function editSwitch(existing, onSaved, preset) {
    let pins = [], all = [];
    try {
      [pins, all] = await Promise.all([api.get('/api/gpio/pins').then((r) => r.pins), api.get('/api/gpio/switches').then((r) => r.switches)]);
    } catch (e) { return UI.notifyError(e); }
    preset = preset || {};
    const boardDefault = all.length ? all[0].active_high : false;
    const sw = Object.assign({ name: '', pin: null, mode: 'toggle', active_high: boardDefault, pulse_ms: 500, max_on_seconds: null, interlock_group: '', icon: '', color: '', confirm: false, enabled: true, notes: '' }, preset, existing || {});
    const isNew = !existing;
    let kind = sw.mode;
    // A new motor button starts as an Up + Down pair; a copy or a "matching
    // button" is a single one by design.
    const canPair = isNew && preset.pair !== false && !preset.partner && !preset.name;
    let pairMode = canPair;
    let limitTouched = sw.max_on_seconds != null;
    if (sw.max_on_seconds == null) sw.max_on_seconds = DEFAULT_LIMIT[kind];
    let color = sw.color, iconName = sw.icon;
    const body = h('div', { class: 'stack switch-editor' });

    // 1. What it does
    const cards = h('div', { class: 'choice-cards', role: 'radiogroup', 'aria-label': 'What will this button do?' });
    for (const [k, info] of Object.entries(KINDS)) {
      const c = h('button', { type: 'button', class: 'choice', role: 'radio', dataset: { kind: k } },
        h('span', { class: 'choice-icon', html: icon(info.icon) }),
        h('span', { class: 'choice-text' }, h('b', { text: info.title }), h('span', { class: 'choice-tag', text: info.tag }), h('span', { class: 'small muted', text: info.text })));
      c.onclick = () => { kind = k; if (!limitTouched) maxOn.value = DEFAULT_LIMIT[k]; refresh(); };
      cards.append(c);
    }

    // 2. Name(s)
    const name = h('input', { class: 'input', value: sw.name, maxlength: 60 });
    const base = h('input', { class: 'input', value: preset.base || 'Bed', maxlength: 40, placeholder: 'e.g. Bed, Recliner, Awning' });
    const dirs = h('select', { class: 'select' });
    DIRECTIONS.forEach(([a, b], i) => dirs.append(h('option', { value: String(i), text: `${cap(a)} and ${cap(b)}` })));
    const pairPreview = h('div', { class: 'small muted' });
    const pairNames = () => { const [a, b] = DIRECTIONS[+dirs.value]; const t = base.value.trim(); return [`${t} ${a}`.trim(), `${t} ${b}`.trim()]; };
    const pairToggle = h('input', { type: 'checkbox', checked: pairMode });
    pairToggle.onchange = () => { pairMode = pairToggle.checked; refresh(); };

    // 3. Pins
    const board = h('select', { class: 'select' },
      h('option', { value: '0', text: 'Turns on with LOW: most blue relay boards with IN1, IN2… pins', selected: !sw.active_high }),
      h('option', { value: '1', text: 'Turns on with HIGH: solid-state relays and transistor boards', selected: !!sw.active_high }));
    const isHigh = () => board.value === '1';
    const pickA = pinPicker(pins, sw.pin, existing ? existing.pin : null, isHigh, 'GPIO pin');
    const pickB = pinPicker(pins, null, null, isHigh, 'GPIO pin for the second button');
    board.onchange = () => { pickA.redraw(); pickB.redraw(); };
    const pinLabelA = h('span', { class: 'field-label' });
    const pinLabelB = h('span', { class: 'field-label' });

    // Tap length
    const pulseMs = h('input', { class: 'input', type: 'number', min: 20, max: 60000, step: 50, value: sw.pulse_ms });
    const pulseField = UI.field('How long to press it (milliseconds)', pulseMs, '500 is half a second, which suits most remotes and garage door openers.');

    // More options
    const maxOn = h('input', { class: 'input', type: 'number', min: 0, max: 86400, step: 1, value: sw.max_on_seconds });
    maxOn.oninput = () => { limitTouched = true; };
    const groups = [...new Set(all.map((s) => s.interlock_group).filter(Boolean))];
    const listId = 'interlock-groups-' + Math.random().toString(36).slice(2, 8);
    const group = h('input', { class: 'input', value: sw.interlock_group, placeholder: 'e.g. bed', maxlength: 40, list: listId });
    const groupList = h('datalist', { id: listId }, groups.map((g) => h('option', { value: g })));
    const notes = h('input', { class: 'input', value: sw.notes, placeholder: 'e.g. Relay 3, blue wire', maxlength: 200 });
    const confirmChk = h('input', { type: 'checkbox', checked: sw.confirm });
    const enabledChk = h('input', { type: 'checkbox', checked: sw.enabled });
    const groupField = UI.field('Never runs at the same time as', group,
      'Buttons that share the same word here can never be on together, like a motor\'s Up and Down. Leave it empty if this button works on its own.');
    const appearance = h('div', { class: 'stack' }, h('span', { class: 'field-label', text: 'Icon and colour on the dashboard' }),
      UI.iconPicker(iconName || switchIcon(sw), (n) => { iconName = n; }), UI.colorPicker(color, (c) => { color = c; }));
    const more = h('details', { class: 'more-options' },
      h('summary', { text: 'More options' }),
      h('div', { class: 'stack' },
        UI.field('Safety time limit (seconds)', maxOn, 'Turns itself off after this long, even if someone forgets. 60 is one minute, 3600 is an hour, 0 means no limit. Hold-to-run buttons always have a limit.'),
        groupField,
        UI.field('Notes for later', notes, 'Only shown here. Handy for which relay or wire this is.'),
        h('label', { class: 'check' }, confirmChk, h('span', { text: 'Ask "are you sure?" before turning it on' })),
        h('label', { class: 'check' }, enabledChk, h('span', { text: 'Active (untick to switch it off without deleting it)' })),
        appearance), groupList);

    const nameField = UI.field('Name on the button', name);
    const pairBox = h('label', { class: 'check pair-check' }, pairToggle, h('span', {}, h('b', { text: 'It moves two ways. ' }), 'Make a pair of buttons, like Up and Down, that can never run at the same time.'));
    const pairFields = h('div', { class: 'form-grid' }, UI.field('What does it move?', base), UI.field('The two directions', dirs), h('div', { class: 'full' }, pairPreview));
    const secondPin = h('div', { class: 'stack', style: { gap: '4px' } }, pinLabelB, pickB.el);
    const step = (n, title, ...children) => h('section', { class: 'step' }, h('h4', {}, h('span', { class: 'step-num', text: String(n) }), title), ...children);
    const pinHelp = el(`<p class="small muted">Use the <b>GPIO number</b> from a Raspberry Pi pinout diagram, not the pin's position along the header. Follow the wire from the relay board's <b>IN</b> pin back to the Pi. Press <b>Test</b> to click the relay and check.</p>`);

    body.append(
      step(1, 'What will this button do?', cards, canPair ? pairBox : null),
      step(2, 'What is it called?', nameField, pairFields),
      step(3, 'Which relay is it wired to?', pinHelp,
        h('div', { class: 'stack', style: { gap: '4px' } }, pinLabelA, pickA.el), secondPin,
        UI.field('What kind of relay board?', board, 'Not sure? If a relay\'s light is on while nothing should be running, choose the other option.'),
        pulseField),
      more);

    function refresh() {
      cards.querySelectorAll('.choice').forEach((c) => { const on = c.dataset.kind === kind; c.classList.toggle('active', on); c.setAttribute('aria-checked', on ? 'true' : 'false'); });
      const pair = pairMode && kind === 'momentary' && canPair;
      pairBox.classList.toggle('hidden', kind !== 'momentary');
      nameField.classList.toggle('hidden', pair);
      pairFields.classList.toggle('hidden', !pair);
      secondPin.classList.toggle('hidden', !pair);
      pulseField.classList.toggle('hidden', kind !== 'pulse');
      groupField.classList.toggle('hidden', pair);
      appearance.classList.toggle('hidden', pair);
      const [a, b] = pairNames();
      pairPreview.innerHTML = `Adds two buttons: <b>${esc(a)}</b> and <b>${esc(b)}</b>. Each one only runs while it is held, and they can never run together.`;
      pinLabelA.textContent = pair ? `Relay for “${a}”` : 'GPIO number';
      pinLabelB.textContent = `Relay for “${b}”`;
      name.placeholder = { momentary: 'e.g. Bed up', toggle: 'e.g. Reading lamp', pulse: 'e.g. Garage door' }[kind];
      save.textContent = existing ? 'Save changes' : pair ? 'Add both buttons' : 'Add button';
      if (kind === 'momentary' && +maxOn.value === 0) maxOn.value = 60;
    }
    base.oninput = refresh; dirs.onchange = refresh;

    const save = el(`<button class="btn primary" data-busy="Saving…"></button>`);
    const cancel = el(`<button class="btn">Cancel</button>`);
    refresh();
    const m = UI.modal({ title: existing ? `Edit “${existing.name}”` : 'Add a button', icon: 'switch', body, footer: [cancel, save], wide: true, sticky: true });
    cancel.onclick = m.close;
    save.onclick = () => UI.busy(save, (async () => {
      const common = { mode: kind, active_high: isHigh(), pulse_ms: +pulseMs.value, max_on_seconds: +maxOn.value, confirm: confirmChk.checked, enabled: enabledChk.checked, notes: notes.value.trim() };
      if (canPair && pairMode && kind === 'momentary') {
        const [a, b] = pairNames();
        if (!base.value.trim()) throw new Error('Say what the motor moves, for example "Bed"');
        if (pickA.value == null || pickB.value == null) throw new Error('Choose a GPIO number for both buttons');
        if (pickA.value === pickB.value) throw new Error('The two buttons need different GPIO numbers: each one drives its own relay');
        let grp = slug(base.value) || 'motor', n = 2;
        while (groups.includes(grp)) grp = `${slug(base.value) || 'motor'}-${n++}`;
        const [wa, wb] = DIRECTIONS[+dirs.value];
        const one = (nm, pin, w) => ({ ...common, name: nm, pin, interlock_group: grp, icon: DIR_ICON[w.split(' ').pop()] || 'hand', color });
        await api.post('/api/gpio/switches', { switches: [one(a, pickA.value, wa), one(b, pickB.value, wb)] });
        UI.toast(`Added ${a} and ${b}`, 'success');
      } else {
        const payload = { ...common, name: name.value.trim(), pin: pickA.value, interlock_group: group.value.trim(), icon: iconName, color };
        if (!payload.name) throw new Error('Give the button a name');
        if (payload.pin == null) throw new Error('Choose the GPIO number the relay is wired to');
        if (existing) await api.put(`/api/gpio/switches/${existing.id}`, payload);
        else {
          // Pair with an existing switch: it joins the same group first, so
          // the two are never both on, even for a moment.
          const partner = preset.partner;
          if (partner && payload.interlock_group && partner.interlock_group !== payload.interlock_group) {
            await api.put(`/api/gpio/switches/${partner.id}`, { interlock_group: payload.interlock_group });
          }
          await api.post('/api/gpio/switches', payload);
        }
        UI.toast(existing ? 'Saved' : `Added ${payload.name}`, 'success');
      }
      m.close(); onSaved && onSaved(); if (window.App) App.refresh(true);
    })().catch(UI.notifyError));
    return m;
  }

  /** Pre-fill for "Add the matching Down button" next to a lone hold-to-run switch. */
  function partnerPreset(sw) {
    const opp = opposite(sw.name);
    const grp = sw.interlock_group || slug(sw.name.replace(/\s+\S+$/, '')) || slug(sw.name) || 'motor';
    return { mode: 'momentary', name: opp ? opp.name : `${sw.name} (other way)`, active_high: sw.active_high, max_on_seconds: sw.max_on_seconds, interlock_group: grp, icon: (opp && opp.icon) || sw.icon, color: sw.color, partner: sw };
  }

  /** Pre-fill for "Copy": everything but the pin. */
  function copyPreset(sw) {
    const { id, pin, ...rest } = sw;
    return { ...rest, name: `${sw.name} copy`.slice(0, 60), pair: false };
  }

  async function removeSwitch(sw, onDone) {
    const ok = await UI.confirm({ title: 'Remove switch', message: `Remove "${sw.name}" (GPIO${sw.pin})? The relay will be turned off.`, okText: 'Remove', danger: true });
    if (!ok) return;
    try { await api.del(`/api/gpio/switches/${sw.id}`); UI.toast('Switch removed', 'success'); onDone && onDone(); } catch (e) { UI.notifyError(e); }
  }

  window.GPIO = { HoldController, control, editSwitch, removeSwitch, switchIcon, MODE_LABEL, KINDS, opposite, partnerPreset, copyPreset, slug };
})();
