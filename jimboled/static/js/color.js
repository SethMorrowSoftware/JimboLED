/* Colour maths and the wheel picker used for WLED colour slots. */
(function () {
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  function hsvToRgb(h, s, v) {
    h = ((h % 360) + 360) % 360; s = clamp(s, 0, 1); v = clamp(v, 0, 1);
    const c = v * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60) [r, g, b] = [c, x, 0]; else if (h < 120) [r, g, b] = [x, c, 0]; else if (h < 180) [r, g, b] = [0, c, x];
    else if (h < 240) [r, g, b] = [0, x, c]; else if (h < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
    return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
  }
  function rgbToHsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    let hh = 0;
    if (d) { if (max === r) hh = ((g - b) / d) % 6; else if (max === g) hh = (b - r) / d + 2; else hh = (r - g) / d + 4; }
    hh = Math.round(hh * 60); if (hh < 0) hh += 360;
    return [hh, max ? d / max : 0, max];
  }
  const rgbToHex = (rgb) => '#' + rgb.slice(0, 3).map((c) => clamp(Math.round(c || 0), 0, 255).toString(16).padStart(2, '0')).join('');
  function hexToRgb(hex) { hex = (hex || '').replace('#', ''); if (hex.length === 3) hex = hex.split('').map((c) => c + c).join(''); if (!/^[0-9a-f]{6}$/i.test(hex)) return null; return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]; }
  function kelvinToRgb(k) {
    const t = clamp(k, 1000, 40000) / 100; let r, g, b;
    r = t <= 66 ? 255 : clamp(329.7 * Math.pow(t - 60, -0.1332), 0, 255);
    g = t <= 66 ? clamp(99.47 * Math.log(t) - 161.1, 0, 255) : clamp(288.1 * Math.pow(t - 60, -0.0755), 0, 255);
    b = t >= 66 ? 255 : t <= 19 ? 0 : clamp(138.5 * Math.log(t - 10) - 305, 0, 255);
    return [Math.round(r), Math.round(g), Math.round(b)];
  }
  function css(col) {
    if (!Array.isArray(col)) return '#000';
    const [r = 0, g = 0, b = 0, w = 0] = col;
    if (w && !r && !g && !b) return `rgb(${255 * w / 255},${240 * w / 255},${210 * w / 255})`;
    if (w) { const f = w / 255; return `rgb(${Math.min(255, r + 255 * f * .6)},${Math.min(255, g + 240 * f * .6)},${Math.min(255, b + 210 * f * .6)})`; }
    return `rgb(${r},${g},${b})`;
  }
  const isDark = (col) => Array.isArray(col) && (col[0] + col[1] + col[2] + (col[3] || 0)) < 60;
  const QUICK = ['#ff0000', '#ff4000', '#ff8000', '#ffbf00', '#ffff00', '#80ff00', '#00ff00', '#00ff80', '#00ffff', '#0080ff', '#0000ff', '#8000ff', '#ff00ff', '#ff0080', '#ffffff', '#ffe0b0', '#ffb070', '#ff9040', '#404040', '#000000'];

  /** Build a picker.  opts: {rgb:[r,g,b,(w)], hasWhite, hasCct, cct, onChange(rgbw, cct)} */
  function picker(opts) {
    const state = { rgb: (opts.rgb || [255, 160, 0]).slice(0, 3), w: (opts.rgb && opts.rgb[3]) || 0, cct: opts.cct ?? 127 };
    let [hue, sat, val] = rgbToHsv(...state.rgb);
    if (val === 0) { val = 1; }
    const root = UI.h('div', { class: 'picker' });
    const wrap = UI.h('div', { class: 'wheel-wrap' });
    const canvas = UI.h('canvas', { class: 'wheel', width: 240, height: 240 });
    const cursor = UI.h('div', { class: 'wheel-cursor' });
    wrap.append(canvas, cursor);
    root.append(wrap);
    const ctx = canvas.getContext('2d');
    const R = 120;
    function drawWheel() {
      const img = ctx.createImageData(240, 240);
      for (let y = 0; y < 240; y++) for (let x = 0; x < 240; x++) {
        const dx = x - R, dy = y - R, d = Math.sqrt(dx * dx + dy * dy);
        const i = (y * 240 + x) * 4;
        if (d > R) { img.data[i + 3] = 0; continue; }
        const ang = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
        const [r, g, b] = hsvToRgb(ang, d / R, val);
        img.data[i] = r; img.data[i + 1] = g; img.data[i + 2] = b; img.data[i + 3] = d > R - 1.5 ? 255 * (R - d) / 1.5 : 255;
      }
      ctx.putImageData(img, 0, 0);
    }
    function placeCursor() {
      const a = hue * Math.PI / 180, d = sat * R;
      cursor.style.left = (R + Math.cos(a) * d) + 'px'; cursor.style.top = (R + Math.sin(a) * d) + 'px';
      cursor.style.background = rgbToHex(state.rgb);
    }
    const emit = UI.throttle(() => opts.onChange && opts.onChange([...state.rgb, state.w], state.cct), 120);
    function updateFromHsv(e) { state.rgb = hsvToRgb(hue, sat, val); hexInput.value = rgbToHex(state.rgb); placeCursor(); preview.style.background = css([...state.rgb, state.w]); if (e !== false) emit(); }
    let dragging = false;
    function pick(ev) {
      const r = canvas.getBoundingClientRect(); const s = 240 / r.width;
      const x = (ev.clientX - r.left) * s - R, y = (ev.clientY - r.top) * s - R;
      const d = Math.min(R, Math.sqrt(x * x + y * y));
      hue = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360; sat = d / R;
      updateFromHsv();
    }
    wrap.addEventListener('pointerdown', (e) => { dragging = true; wrap.setPointerCapture(e.pointerId); pick(e); e.preventDefault(); });
    wrap.addEventListener('pointermove', (e) => { if (dragging) pick(e); });
    wrap.addEventListener('pointerup', () => { dragging = false; });
    wrap.addEventListener('pointercancel', () => { dragging = false; });

    const preview = UI.h('div', { class: 'swatch lg', style: { background: css([...state.rgb, state.w]), cursor: 'default' } });
    const hexInput = UI.h('input', { class: 'input mono', value: rgbToHex(state.rgb), maxlength: 7, spellcheck: 'false', style: { width: '110px' } });
    hexInput.addEventListener('change', () => { const rgb = hexToRgb(hexInput.value); if (!rgb) { hexInput.classList.add('invalid'); return; } hexInput.classList.remove('invalid'); [hue, sat, val] = rgbToHsv(...rgb); if (val === 0) val = 0.001; state.rgb = rgb; drawWheel(); placeCursor(); preview.style.background = css([...state.rgb, state.w]); valSlider.setValue(Math.round(val * 100)); emit(); });
    root.append(UI.h('div', { class: 'row' }, preview, hexInput, UI.h('span', { class: 'muted small grow', text: 'Drag on the wheel, or type a hex value' })));

    const valSlider = UI.slider({ label: 'Colour brightness', min: 1, max: 100, value: Math.round(val * 100), format: (v) => v + '%', onInput: (v) => { val = v / 100; drawWheel(); updateFromHsv(); } });
    root.append(valSlider);
    if (opts.hasWhite) {
      const wSlider = UI.slider({ label: 'White channel', min: 0, max: 255, value: state.w, fill: '#f5e6c8', onInput: (v) => { state.w = v; preview.style.background = css([...state.rgb, state.w]); emit(); } });
      root.append(wSlider);
    }
    if (opts.hasCct) {
      const cSlider = UI.slider({ label: 'White temperature', min: 0, max: 255, value: state.cct, fill: 'linear-gradient(90deg,#ffb46b,#fff,#bcd7ff)', format: (v) => (v < 85 ? 'warm' : v > 170 ? 'cool' : 'neutral'), onInput: (v) => { state.cct = v; emit(); } });
      root.append(cSlider);
    }
    const quick = UI.h('div', { class: 'preset-colors' });
    for (const hx of QUICK) {
      const b = UI.h('button', { class: 'swatch', style: { background: hx }, title: hx });
      b.onclick = () => { const rgb = hexToRgb(hx); [hue, sat, val] = rgbToHsv(...rgb); if (val === 0) val = 0.001; state.rgb = rgb; hexInput.value = hx; drawWheel(); placeCursor(); preview.style.background = css([...state.rgb, state.w]); valSlider.setValue(Math.round(val * 100)); emit(); };
      quick.append(b);
    }
    root.append(quick);
    drawWheel(); placeCursor();
    root.getValue = () => ({ rgbw: [...state.rgb, state.w], cct: state.cct });
    return root;
  }
  window.Color = { hsvToRgb, rgbToHsv, rgbToHex, hexToRgb, kelvinToRgb, css, isDark, picker, QUICK };
})();
