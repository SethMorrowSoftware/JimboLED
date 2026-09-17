/* Emergency stop: the header button, the latched banner and the zone sheet.
 *
 * Engaging is one tap and never asks a question you have to read – in an
 * emergency, a confirmation dialog is an obstacle.  Resetting is the guarded
 * direction: it names what it is about to unlock and says so out loud.
 */
(function () {
  const { h, el, esc } = UI;

  function zones() { return ((App.state.gpio.estop || {}).zones) || []; }
  function engagedZones() { return zones().filter((z) => z.engaged); }
  function master() { return zones().find((z) => z.id === 'all'); }

  async function engage(zoneId, reason) {
    // Off by default: in an emergency a dialog is an obstacle. Some people
    // still want one against a pocket-tap, so it is theirs to turn on.
    if ((App.state.gpio.estop || {}).confirm_engage) {
      const zone = zones().find((z) => z.id === zoneId);
      const ok = await UI.confirm({
        title: 'Emergency stop',
        message: `Cut and lock out ${zone ? `\u201c${zone.name}\u201d` : 'every relay'}?`,
        okText: 'Stop now', danger: true,
      });
      if (!ok) return false;
    }
    try {
      const r = await api.post('/api/estop/engage', { zone: zoneId, reason: reason || '' });
      if (navigator.vibrate) navigator.vibrate([40, 60, 40]);
      const stopped = (r.stopped || []).length;
      UI.toast(stopped ? `Stopped: ${r.stopped.join(', ')}. Everything stays locked until you reset it.`
                       : 'Emergency stop engaged. Relays stay locked until you reset it.', 'error', 6000);
      App.refresh(true);
      return true;
    } catch (err) { UI.notifyError(err); return false; }
  }

  async function reset(zone) {
    const covers = (zone.switches || []).length;
    const ok = await UI.confirm({
      title: `Reset “${zone.name}”?`,
      message: covers
        ? `This unlocks ${covers} switch${covers === 1 ? '' : 'es'}. Nothing switches on by itself – but make sure whatever caused the stop has been dealt with first.`
        : 'This clears the emergency stop. Nothing switches on by itself.',
      okText: 'Unlock',
    });
    if (!ok) return;
    try {
      await api.post('/api/estop/reset', { zone: zone.id });
      UI.toast(`“${zone.name}” unlocked`, 'success');
      App.refresh(true);
    } catch (err) { UI.notifyError(err); }
  }

  /** The header button. One tap stops the master zone; long-press picks a zone. */
  function headerButton() {
    const btn = el(`<button class="estop-btn" type="button"></button>`);
    let heldTimer = null, longPressed = false;
    const openSheet = () => { longPressed = true; sheet(); };
    btn.addEventListener('pointerdown', () => { heldTimer = setTimeout(openSheet, 550); });
    const clear = () => { clearTimeout(heldTimer); heldTimer = null; };
    btn.addEventListener('pointerup', clear);
    btn.addEventListener('pointerleave', clear);
    btn.addEventListener('pointercancel', clear);
    btn.addEventListener('contextmenu', (e) => { e.preventDefault(); openSheet(); });
    btn.onclick = () => {
      if (longPressed) { longPressed = false; return; }
      if (engagedZones().length) return sheet();
      if (zones().length > 1) return sheet();
      engage('all');
    };
    btn.update = () => {
      const latched = engagedZones();
      btn.classList.toggle('latched', latched.length > 0);
      const label = latched.length ? 'Stopped' : 'E-stop';
      btn.title = latched.length
        ? `Emergency stop engaged: ${latched.map((z) => z.name).join(', ')}. Tap to reset.`
        : (zones().length > 1 ? 'Emergency stop – tap to choose what to stop' : 'Emergency stop – cut power to every relay and lock them out');
      btn.setAttribute('aria-label', latched.length ? `Emergency stop engaged, tap to reset` : 'Emergency stop');
      btn.innerHTML = icon(latched.length ? 'lock' : 'estop') + `<span class="hide-sm">${label}</span>`;
    };
    btn.update();
    return btn;
  }

  /** The banner that sits above the dashboard while anything is latched. */
  function banner() {
    const latched = engagedZones();
    if (!latched.length) return null;
    const node = h('div', { class: 'estop-banner', role: 'alert' });
    const names = latched.map((z) => z.name).join(', ');
    const reasons = latched.map((z) => z.reason).filter(Boolean);
    const hw = latched.flatMap((z) => z.blocked_by || []);
    let detail = latched.length === 1
      ? `${(latched[0].switches || []).length} switch${(latched[0].switches || []).length === 1 ? '' : 'es'} locked out. Nothing can be switched on until you reset it.`
      : 'Several zones are locked out. Nothing they cover can be switched on until you reset them.';
    // A stop held down by hardware cannot be reset from here, so say why
    // rather than offering a button that will only be refused.
    const blockedBy = latched.map((z) => z.blocked_reason).filter(Boolean);
    if (blockedBy.length) detail = `Cannot reset yet \u2013 ${blockedBy.join('; ')}.`;
    else if (reasons.length) detail += ` Reason: ${reasons.join('; ')}.`;
    node.append(
      el(icon('estop')),
      h('div', { class: 'grow' },
        h('div', { class: 'headline', text: `Emergency stop engaged — ${names}` }),
        h('div', { class: 'detail', text: detail })),
    );
    const resetBtn = el(`<button class="btn" type="button">${icon('unlock')} Reset${latched.length > 1 ? '…' : ''}</button>`);
    resetBtn.onclick = () => (latched.length === 1 ? reset(latched[0]) : sheet());
    resetBtn.disabled = hw.length > 0 && latched.length === 1;
    node.append(resetBtn);
    return node;
  }

  /** Full picker: stop or reset any zone, with what each one covers. */
  function sheet() {
    const body = h('div', { class: 'stack' });
    const swNames = Object.fromEntries((App.state.gpio.switches || []).map((s) => [s.id, s.name]));
    const list = h('div', { class: 'stack' });

    function draw() {
      list.innerHTML = '';
      for (const z of zones()) {
        const row = h('div', { class: 'estop-zone' + (z.engaged ? ' engaged' : '') });
        const covered = (z.switches || []).map((id) => swNames[id]).filter(Boolean);
        row.append(el(`<span class="tile-icon">${icon(z.engaged ? 'lock' : (z.icon || 'estop'))}</span>`));
        const meta = z.engaged
          ? `Engaged ${UI.fmtAgo(z.since)}${z.reason ? ` \u00b7 ${z.reason}` : ''}${z.blocked_reason ? ` \u00b7 ${z.blocked_reason}` : ''}`
          : (covered.length ? `Covers ${covered.join(', ')}` : 'Covers nothing yet');

        row.append(h('div', { class: 'grow' },
          h('span', { class: 'title', text: z.name }),
          h('span', { class: 'meta', text: meta })));
        if (z.engaged) {
          const b = el(`<button class="btn sm" type="button">${icon('unlock')} Reset</button>`);
          b.disabled = (z.blocked_by || []).length > 0;
          b.onclick = async () => { await reset(z); m.close(); };
          row.append(b);
        } else {
          const b = el(`<button class="btn sm danger" type="button">${icon('estop')} Stop</button>`);
          b.onclick = async () => { if (await engage(z.id)) { m.close(); } };
          row.append(b);
        }
        list.append(row);
      }
    }
    draw();
    body.append(
      h('p', { class: 'muted small',
               text: 'An emergency stop cuts its relays immediately and keeps them locked out — from every phone, every scene — until it is reset here.' }),
      list);
    if (!zones().some((z) => z.id !== 'all')) {
      body.append(el(`<div class="alert info">Want a stop for just the bed or just the awning? Add a zone under <a href="#/settings/estop">Settings → Emergency stop</a>.</div>`));
    }
    const close = el(`<button class="btn">Close</button>`);
    const m = UI.modal({ title: 'Emergency stop', icon: 'estop', tone: 'danger', body, footer: [close] });
    close.onclick = m.close;
    return m;
  }

  window.EStop = { headerButton, banner, sheet, engage, reset, engagedZones, zones };
})();
