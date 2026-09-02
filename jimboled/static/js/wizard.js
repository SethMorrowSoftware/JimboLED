/* First-run setup wizard. */
(function () {
  const { h, el, esc } = UI;
  function run(onDone) {
    let step = 0;
    const steps = ['Welcome', 'Find your lights', 'Switches', 'Done'];
    const body = h('div', { class: 'stack' });
    const bar = h('div', { class: 'wizard-steps' });
    const content = h('div', { class: 'stack' });
    body.append(bar, content);
    const back = el(`<button class="btn">Back</button>`);
    const next = el(`<button class="btn primary">Next</button>`);
    const m = UI.modal({ title: 'Set up JimboLED', body, footer: [back, next], wide: true, sticky: true, onClose: () => finish(false) });
    let finished = false;
    async function finish(save) {
      if (finished) return; finished = true;
      if (save) { try { await api.put('/api/settings', { setup_complete: true }); } catch (e) { UI.notifyError(e); } }
      onDone && onDone();
    }
    function draw() {
      bar.innerHTML = ''; steps.forEach((s, i) => bar.append(h('span', { class: i <= step ? 'done' : '' })));
      content.innerHTML = '';
      back.classList.toggle('hidden', step === 0);
      next.textContent = step === steps.length - 1 ? 'Open the dashboard' : (step === 2 ? 'Next' : 'Next');
      if (step === 0) {
        content.append(el(`<div class="hero"><span class="brand-mark"></span><h2>Welcome to JimboLED</h2><p>Let's connect your WLED lights and any relay switches wired to this Raspberry Pi. It only takes a minute, and you can change everything later in Settings.</p></div>`));
      } else if (step === 1) {
        const list = h('div', { class: 'list' });
        const status = h('div', { class: 'row small muted' }, el(icon('refresh', 'spin')), h('span', { text: 'Searching your network for WLED controllers…' }));
        const manual = el(`<button class="btn">${icon('plus')} Add by IP address instead</button>`);
        manual.onclick = () => Device.addDeviceDialog(() => poll());
        content.append(el(`<p class="muted">Make sure your WLED controllers are powered on and connected to the same Wi‑Fi network as this Pi.</p>`), status, list, h('div', { class: 'row' }, manual));
        let timer;
        async function poll() {
          clearTimeout(timer); if (step !== 1 || !m.el.isConnected) return;
          let s; try { s = await api.get('/api/discover'); } catch (e) { return; }
          const all = s.found.slice(); for (const p of s.passive || []) if (!all.some((f) => f.host === p.host)) all.push({ host: p.host, name: p.name, known: false });
          status.innerHTML = s.running ? `${icon('refresh', 'spin')} <span>${esc(s.progress || 'Searching')}…</span>` : `${icon('check')} <span>${all.length ? `Found ${all.length} controller${all.length === 1 ? '' : 's'}.` : 'No controllers found yet. You can add one by address, or skip this step and come back later.'}</span>`;
          list.innerHTML = '';
          for (const f of all) {
            const item = h('div', { class: 'list-item' });
            item.append(el(`<span class="tile-icon">${icon('bulb')}</span>`), h('div', { class: 'grow' }, h('span', { class: 'title', text: f.name || f.host }), h('span', { class: 'sub', text: `${f.host}${f.ver ? ' · WLED ' + f.ver : ''}${f.led_count ? ' · ' + f.led_count + ' LEDs' : ''}` })));
            if (f.known) item.append(el(`<span class="badge ok">added</span>`));
            else { const b = el(`<button class="btn sm primary" data-busy="Adding…">${icon('plus')} Add</button>`); b.onclick = () => UI.busy(b, api.post('/api/devices', { host: f.host, name: f.name }).then(() => { f.known = true; poll(); }).catch(UI.notifyError)); item.append(b); }
            list.append(item);
          }
          if (s.running) timer = setTimeout(poll, 1200);
        }
        api.post('/api/discover', {}).then(poll).catch(UI.notifyError);
      } else if (step === 2) {
        content.append(el(`<p class="muted">If you've wired relays to this Pi's GPIO pins – for example to raise and lower an adjustable bed – add them here. Skip this if you only use JimboLED for lights.</p>`));
        const list = h('div', { class: 'list' });
        const bed = el(`<button class="btn primary">${icon('bed')} Set up an adjustable bed (up / down)</button>`);
        bed.onclick = () => Settings.bedTemplate(drawList);
        const one = el(`<button class="btn">${icon('plus')} Add a single switch</button>`);
        one.onclick = () => GPIO.editSwitch(null, drawList);
        content.append(h('div', { class: 'row wrap' }, bed, one), list);
        async function drawList() {
          list.innerHTML = '';
          const r = await api.get('/api/gpio');
          if (r.simulated) list.append(el(`<div class="sim-banner">${icon('alert')}<div>GPIO hardware isn't available here (${esc(r.backend)}), so switches run in simulation. That's fine for trying things out.</div></div>`));
          for (const s of r.switches) list.append(h('div', { class: 'list-item' }, el(`<span class="tile-icon">${icon(GPIO.switchIcon(s))}</span>`), h('div', { class: 'grow' }, h('span', { class: 'title', text: s.name }), h('span', { class: 'sub', text: `GPIO${s.pin} · ${GPIO.MODE_LABEL[s.mode]}` })), el(`<span class="badge ok">ready</span>`)));
        }
        drawList();
      } else {
        content.append(el(`<div class="hero"><span class="brand-mark"></span><h2>You're all set</h2><p>Your dashboard is ready. Use <b>Edit layout</b> to arrange tiles the way you like, and <b>Settings</b> to add more lights, switches or scenes any time.</p><p class="small">Tip: on a phone, add this page to your home screen for a full-screen app.</p></div>`));
      }
    }
    back.onclick = () => { step = Math.max(0, step - 1); draw(); };
    next.onclick = () => { if (step === steps.length - 1) { finish(true); m.close(); } else { step++; draw(); } };
    draw();
    return m;
  }
  window.Wizard = { run };
})();
