// Shared visuals: the sparkle backdrop and the liquid-glass lens.
(function () {
  const $ = s => document.querySelector(s);
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.reduceMotion = reduceMotion;

  // ---------- sparkles ----------
  const c = $("#sky"), ctx = c.getContext("2d");
  let stars = [], w, h;
  function seed() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    w = innerWidth; h = innerHeight;
    c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    stars = Array.from({ length: Math.round(w * h / 5200) }, () => {
      const big = Math.random() < .06;
      return { x: Math.random() * w, y: Math.random() * h, r: big ? 1 + Math.random() * .8 : .3 + Math.random() * .7,
               a: .18 + Math.random() * .6, p: Math.random() * Math.PI * 2, s: .4 + Math.random() * 1.4, big,
               tint: 200 + Math.random() * 55 };
    });
  }
  function glint(s, a, L, horizontal) {
    const g = horizontal ? ctx.createLinearGradient(s.x - L, s.y, s.x + L, s.y) : ctx.createLinearGradient(s.x, s.y - L, s.x, s.y + L);
    g.addColorStop(0, "rgba(230,236,255,0)"); g.addColorStop(.5, `rgba(235,240,255,${a * .8})`); g.addColorStop(1, "rgba(230,236,255,0)");
    ctx.fillStyle = g;
    horizontal ? ctx.fillRect(s.x - L, s.y - .35, L * 2, .7) : ctx.fillRect(s.x - .35, s.y - L, .7, L * 2);
  }
  function draw(t) {
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      const tw = reduceMotion ? 1 : .55 + .45 * Math.sin(t / 1000 * s.s + s.p);
      const a = s.a * tw, v = s.tint | 0;
      ctx.fillStyle = `rgba(${v},${Math.min(255, v + 4)},255,${a})`;
      ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
      if (s.big && tw > .7) { glint(s, a, s.r * 6 * tw, true); glint(s, a, s.r * 6 * tw, false); }
    }
  }
  let last = 0;
  function loop(t) { if (t - last > 33) { draw(t); last = t; } requestAnimationFrame(loop); }
  seed(); addEventListener("resize", seed);
  reduceMotion ? draw(0) : requestAnimationFrame(loop);

  // ---------- liquid-glass lens (Chromium only; Safari keeps the blur + rim) ----------
  const canRefract = CSS.supports("backdrop-filter", "url(#x) blur(1px)") && !/^((?!chrome|android).)*safari/i.test(navigator.userAgent);
  if (canRefract) document.documentElement.classList.add("refract");
  const sizes = new WeakMap();
  function lensFor(el) {
    const w = Math.round(el.offsetWidth), h = Math.round(el.offsetHeight);
    if (!w || !h || sizes.get(el) === `${w}x${h}`) return;
    sizes.set(el, `${w}x${h}`);
    // Displacement map: neutral in the middle, pushed outward near the rounded edge,
    // so what's behind bends around the corners like thick glass.
    const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
    const g = cv.getContext("2d"), img = g.createImageData(w, h), R = 22, edge = 18;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const qx = Math.max(R - x, 0, x - (w - 1 - R)), qy = Math.max(R - y, 0, y - (h - 1 - R));
      const d = (qx > 0 && qy > 0) ? R - Math.hypot(qx, qy) : Math.min(x, y, w - 1 - x, h - 1 - y);
      const k = d < edge ? Math.pow(1 - Math.max(d, 0) / edge, 2) : 0;
      const i = (y * w + x) * 4;
      img.data[i] = 128 + 127 * k * (x - w / 2) / (w / 2);
      img.data[i + 1] = 128 + 127 * k * (y - h / 2) / (h / 2);
      img.data[i + 2] = 128; img.data[i + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    const id = el.dataset.lens || (el.dataset.lens = "lens" + Math.random().toString(36).slice(2, 8));
    let f = document.getElementById(id);
    if (!f) {
      f = document.createElementNS("http://www.w3.org/2000/svg", "filter");
      f.id = id; f.setAttribute("x", "0"); f.setAttribute("y", "0"); f.setAttribute("width", "100%"); f.setAttribute("height", "100%");
      f.setAttribute("color-interpolation-filters", "sRGB");
      $("#lenses").appendChild(f);
    }
    f.innerHTML = `<feImage href="${cv.toDataURL()}" x="0" y="0" width="${w}" height="${h}" result="map" preserveAspectRatio="none"/>
      <feDisplacementMap in="SourceGraphic" in2="map" scale="-46" xChannelSelector="R" yChannelSelector="G"/>`;
    el.style.setProperty("--lens", `url(#${id})`);
  }
  const ro = canRefract ? new ResizeObserver(es => es.forEach(e => lensFor(e.target))) : null;
  window.glassUp = (root = document) => { if (ro) root.querySelectorAll(".glass").forEach(el => ro.observe(el)); };
})();
