/* GPIO switch tiles, hold-to-run controller and the switch editor. */
(function () {
  const { h, el, esc } = UI;
  const MODE_LABEL = { toggle: 'On / off', momentary: 'Hold to run', pulse: 'Tap' };
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
  function editSwitch(existing, onSaved) {
    const sw = Object.assign({ name: '', pin: null, mode: 'toggle', active_high: true, pulse_ms: 500, max_on_seconds: 0, interlock_group: '', icon: '', color: '', confirm: false, enabled: true, notes: '' }, existing || {});
    if (!existing) sw.max_on_seconds = 60;
    const body = h('div', { class: 'stack' });
    const name = h('input', { class: 'input', value: sw.name, placeholder: 'e.g. Bed head up', maxlength: 60 });
    const mode = h('select', { class: 'select' });
    for (const [k, v] of Object.entries(MODE_LABEL)) mode.append(h('option', { value: k, text: v, selected: k === sw.mode }));
    const modeHint = h('span', { class: 'hint' });
    const setHint = () => { modeHint.textContent = { toggle: 'Stays on until you turn it off (or the time limit is reached). Good for lamps, fans, heaters.', momentary: 'Only runs while the button is held down – the safest choice for motors like an adjustable bed.', pulse: 'Energises the relay briefly then releases, like tapping a button on a remote.' }[mode.value]; };
    setHint(); mode.onchange = () => { setHint(); refreshVisibility(); };
    const activeHigh = h('select', { class: 'select' }, h('option', { value: '1', text: 'Active HIGH – relay turns on when the pin is 3.3 V (most SSR / transistor boards)', selected: sw.active_high }), h('option', { value: '0', text: 'Active LOW – relay turns on when the pin is 0 V (most cheap blue relay modules)', selected: !sw.active_high }));
    activeHigh.onchange = () => renderPins();
    const maxOn = h('input', { class: 'input', type: 'number', min: 0, max: 86400, step: 1, value: sw.max_on_seconds });
    const pulseMs = h('input', { class: 'input', type: 'number', min: 20, max: 60000, step: 10, value: sw.pulse_ms });
    const group = h('input', { class: 'input', value: sw.interlock_group, placeholder: 'e.g. bed-head', maxlength: 40 });
    const notes = h('input', { class: 'input', value: sw.notes, placeholder: 'Wiring notes for future you', maxlength: 200 });
    const confirmChk = h('input', { type: 'checkbox', checked: sw.confirm });
    const enabledChk = h('input', { type: 'checkbox', checked: sw.enabled });
    let color = sw.color, iconName = sw.icon;

    // pin picker
    const pinWrap = h('div', { class: 'stack' });
    const pinLabel = h('div', { class: 'row between' }, h('span', { class: 'small muted', text: 'BCM (GPIO) number – the number printed on pinout diagrams, not the physical pin position (#). "boot-safe" pins keep the relay off while the Pi powers up.' }));
    const pinout = h('div', { class: 'pinout' });
    pinWrap.append(pinLabel, pinout);
    let pins = [];
    api.get('/api/gpio/pins').then((r) => { pins = r.pins; renderPins(); }).catch(UI.notifyError);
    function renderPins() {
      pinout.innerHTML = '';
      for (const p of pins) {
        const used = p.in_use_by && (!existing || p.bcm !== existing.pin);
        const b = h('button', { type: 'button', class: 'pin' + (p.recommended ? ' rec' : '') + (p.reserved ? ' reserved' : '') + (used ? ' used' : '') + (p.bcm === sw.pin ? ' selected' : ''), title: p.note + (used ? ` (used by ${p.in_use_by})` : '') });
        const bootOk = (activeHigh.value === '1') ? p.boot_pull === 'down' : p.boot_pull === 'up';
        b.append(h('span', { class: 'num', text: `GPIO${p.bcm}` }), h('span', { class: 'phys', text: `#${p.physical}` }), h('span', { class: 'note grow', text: used ? `in use: ${p.in_use_by}` : p.note }), h('span', { class: 'boot', title: bootOk ? 'Relay stays off during power-up with this trigger type' : 'May click briefly during power-up with this trigger type', text: bootOk ? '✓ boot-safe' : '' }));
        if (!p.reserved && !used) b.onclick = () => { sw.pin = p.bcm; renderPins(); };
        pinout.append(b);
      }
    }
    const testBtn = el(`<button class="btn sm" type="button">${icon('zap')} Test this pin (0.4 s pulse)</button>`);
    testBtn.onclick = async () => { if (sw.pin == null) return UI.toast('Pick a pin first', 'error'); try { await api.post('/api/gpio/test', { pin: sw.pin, active_high: activeHigh.value === '1', duration_ms: 400 }); UI.toast('Pulsed – did the relay click?', 'success'); } catch (e) { UI.notifyError(e); } };

    const groupField = UI.field('Interlock group (optional)', group, 'Switches sharing a group can never be on at the same time. Put "up" and "down" of the same motor in one group.');
    const maxField = UI.field('Auto-off after (seconds, 0 = never)', maxOn, 'A safety limit. Hold-to-run switches always have one.');
    const pulseField = UI.field('Pulse length (milliseconds)', pulseMs);
    function refreshVisibility() { pulseField.classList.toggle('hidden', mode.value !== 'pulse'); if (mode.value === 'momentary' && +maxOn.value === 0) maxOn.value = 60; }
    refreshVisibility();

    body.append(
      h('div', { class: 'form-grid' },
        UI.field('Name', name),
        UI.field('Behaviour', mode, modeHint),
        h('div', { class: 'full' }, UI.field('Relay trigger', activeHigh, 'Not sure? Save, then use "Test this pin" – if the relay clicks ON when you expect OFF, switch this setting.')),
        maxField, pulseField, groupField,
        UI.field('Notes', notes)),
      h('div', { class: 'stack' }, h('h4', { text: 'GPIO pin' }), pinWrap, h('div', { class: 'row' }, testBtn)),
      h('div', { class: 'stack' }, h('h4', { text: 'Appearance' }), UI.iconPicker(iconName || switchIcon(sw), (n) => { iconName = n; }), UI.colorPicker(color, (c) => { color = c; })),
      h('label', { class: 'check' }, confirmChk, h('span', { text: 'Ask for confirmation before turning on' })),
      h('label', { class: 'check' }, enabledChk, h('span', { text: 'Enabled' })),
    );
    const save = el(`<button class="btn primary" data-busy="Saving…">${existing ? 'Save changes' : 'Add switch'}</button>`);
    const cancel = el(`<button class="btn">Cancel</button>`);
    const m = UI.modal({ title: existing ? 'Edit switch' : 'Add a switch', icon: 'switch', body, footer: [cancel, save], wide: true, sticky: true });
    cancel.onclick = m.close;
    save.onclick = () => UI.busy(save, (async () => {
      const payload = { name: name.value.trim(), pin: sw.pin, mode: mode.value, active_high: activeHigh.value === '1', pulse_ms: +pulseMs.value, max_on_seconds: +maxOn.value, interlock_group: group.value.trim(), icon: iconName, color, confirm: confirmChk.checked, enabled: enabledChk.checked, notes: notes.value.trim() };
      if (!payload.name) throw new Error('Give the switch a name');
      if (payload.pin == null) throw new Error('Pick a GPIO pin');
      if (existing) await api.put(`/api/gpio/switches/${existing.id}`, payload); else await api.post('/api/gpio/switches', payload);
      UI.toast(existing ? 'Switch saved' : 'Switch added', 'success');
      m.close(); onSaved && onSaved();
    })().catch(UI.notifyError));
    return m;
  }

  async function removeSwitch(sw, onDone) {
    const ok = await UI.confirm({ title: 'Remove switch', message: `Remove "${sw.name}" (GPIO${sw.pin})? The relay will be turned off.`, okText: 'Remove', danger: true });
    if (!ok) return;
    try { await api.del(`/api/gpio/switches/${sw.id}`); UI.toast('Switch removed', 'success'); onDone && onDone(); } catch (e) { UI.notifyError(e); }
  }

  window.GPIO = { HoldController, control, editSwitch, removeSwitch, switchIcon, MODE_LABEL };
})();
