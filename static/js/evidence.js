/* Evidence highlighting: click a value (or an issue) and the PDF scrolls to where it was read.

   Progressive enhancement. The page works without this file: the browser's own PDF viewer shows the
   document in the iframe. When PDF.js (static/vendor/pdfjs, loaded once the viewer is near the screen or a
   document is asked for, so stacked mobile layouts don't download 1.8 MB up front) is available, this file
   renders the PDF itself and draws boxes from the positions the server found (ExtractedField.location,
   delivered as JSON script tags). Scanned documents without word positions keep the browser viewer.
   CSP-safe: no inline code, no eval; PDF.js and its worker come from our own /static/. */
(function () {
  "use strict";

  var viewer = document.querySelector("[data-evidence-viewer]");
  if (!viewer || !window.Promise || !window.IntersectionObserver || !Element.prototype.closest) { return; }
  var aside = viewer.closest(".viewer");
  if (!aside) { return; }
  var frame = aside.querySelector("iframe");
  var scroller = viewer.querySelector("[data-ev-scroll]");
  var statusEl = viewer.querySelector("[data-ev-status]");
  var liveEl = viewer.querySelector("[data-ev-live]");
  var zoomLabel = viewer.querySelector("[data-ev-zoom-label]");
  var currentEl = viewer.querySelector("[data-ev-current]");
  var countEl = viewer.querySelector("[data-ev-count]");
  var titleEl = document.getElementById("pdf-title") || aside.querySelector(".viewer-head strong");
  var openEl = document.getElementById("pdf-open") || aside.querySelector(".viewer-head a");
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var HINT = "Click a value on the left to see where it was read.";
  var MSG = {
    scanned: "Location not available for scanned pages.",
    failed: "Highlighting isn't available for this PDF, so it is shown in the browser's viewer.",
    typed: "This value was typed in by a reviewer and isn't printed on the page.",
    notFound: "This value wasn't found on the page. Check it against the document.",
    unknown: "Location not available for this value yet."
  };
  var ZOOMS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];
  var PAD = 12;          // space around pages, px (matches .ev-scroll padding)
  var GAP = 12;          // space between pages, px

  // ------------------------------------------------------------------ document data

  var docs = {};
  var order = [];
  document.querySelectorAll('script[type="application/json"][id^="evidence-doc-"]').forEach(function (el) {
    try {
      var d = JSON.parse(el.textContent);
      d.card = el.closest(".doc-card");
      docs[String(d.id)] = d;
      order.push(String(d.id));
    } catch (e) { /* malformed data: that document keeps the plain viewer */ }
  });
  if (!order.length) { return; }

  function docFor(el) {
    var card = el.closest(".doc-card");
    if (card) {
      var s = card.querySelector('script[id^="evidence-doc-"]');
      return s ? docs[s.id.slice("evidence-doc-".length)] : null;
    }
    return order.length === 1 ? docs[order[0]] : null;
  }

  function docByUrl(url) {
    for (var i = 0; i < order.length; i++) { if (docs[order[i]].url === url) { return docs[order[i]]; } }
    return null;
  }

  // ------------------------------------------------------------------ PDF.js

  var pdfjsPromise = null;
  function loadPdfjs() {
    if (!pdfjsPromise) {
      pdfjsPromise = import(viewer.getAttribute("data-pdfjs")).then(function (mod) {
        mod.GlobalWorkerOptions.workerSrc = viewer.getAttribute("data-pdfjs-worker");
        return mod;
      });
    }
    return pdfjsPromise;
  }

  var pdfjs = null;
  var started = null;
  var unavailable = false;
  /* Resolves true once PDF.js is ready; false if it can't load (the page then keeps the browser viewer). */
  function start() {
    if (!started) {
      started = loadPdfjs().then(function (mod) { pdfjs = mod; return true; }, function (err) {
        // No PDF.js (old browser, blocked file): put the page back as it was, with the browser's PDF viewer.
        if (window.console) { console.warn("Evidence viewer unavailable.", err); }
        unavailable = true;
        viewer.hidden = true;
        aside.classList.remove("ev-on", "ev-native");
        if (frame && !frame.isConnected) { aside.appendChild(frame); }
        document.querySelectorAll(".ev-locate").forEach(function (b) { b.remove(); });
        document.querySelectorAll(".ev-row").forEach(function (r) { r.classList.remove("ev-row", "ev-row-on"); });
        document.querySelectorAll("[data-evidence-issue]").forEach(function (b) { b.hidden = true; });
        return false;
      });
    }
    return started;
  }

  var state = { doc: null, pdf: null, task: null, pages: [], zoom: 1, scale: 0, ready: null, generation: 0 };
  var observer = null;

  function wasmUrl() {
    return new URL("wasm/", new URL(viewer.getAttribute("data-pdfjs-worker"), window.location.href)).href;
  }

  function destroyPdf() {
    state.generation += 1;
    if (observer) { observer.disconnect(); observer = null; }
    state.pages.forEach(function (p) { if (p.task) { p.task.cancel(); } });
    state.pages = [];
    if (state.task) { state.task.destroy(); state.task = null; }
    state.pdf = null;
    scroller.textContent = "";
  }

  function useNative(d, message) {
    destroyPdf();
    aside.classList.remove("ev-on");
    aside.classList.add("ev-native");
    if (frame) {
      if (frame.getAttribute("src") !== d.url) { frame.setAttribute("src", d.url); }
      if (!frame.isConnected) { aside.appendChild(frame); }
    }
    setStatus(message, true);
  }

  function usePdfjs() {
    aside.classList.add("ev-on");
    aside.classList.remove("ev-native");
    // Detached, the iframe stops loading; app.js may still set its src, which does nothing until it is re-attached.
    if (frame && frame.isConnected) { frame.remove(); }
  }

  function syncChrome(d) {
    if (titleEl) { titleEl.textContent = d.title; }
    if (openEl) { openEl.href = d.url; }
    if (d.card && !d.card.classList.contains("active")) {
      document.querySelectorAll(".doc-card.active").forEach(function (c) { c.classList.remove("active"); });
      d.card.classList.add("active");
    }
  }

  /* Show a document in the pane. Resolves true when PDF.js shows it (boxes can be drawn). */
  function showDoc(d) {
    if (unavailable) { return Promise.resolve(false); }
    syncChrome(d);
    if (state.doc === d && state.ready) { return state.ready; }
    state.doc = d;
    if (d.mode !== "pdfjs") {
      useNative(d, MSG.scanned);
      state.ready = Promise.resolve(false);
      return state.ready;
    }
    state.ready = start().then(function (ok) {
      if (!ok || state.doc !== d) { return false; }
      return openPdf(d).then(function () { return true; });
    }).then(null, function (err) {
      if (state.doc !== d) { return false; }
      if (window.console) { console.warn("Evidence viewer: falling back to the browser viewer.", err); }
      useNative(d, MSG.failed);
      return false;
    });
    return state.ready;
  }

  function openPdf(d) {
    destroyPdf();
    usePdfjs();
    var generation = state.generation;
    setStatus("Opening " + d.title + "...", false);
    var task = pdfjs.getDocument({ url: d.url, wasmUrl: wasmUrl(), useSystemFonts: true, enableXfa: false });
    state.task = task;
    return task.promise.then(function (pdf) {
      if (generation !== state.generation) { throw new Error("replaced"); }
      state.pdf = pdf;
      return buildPages(pdf, generation);
    }).then(function () {
      setStatus(HINT, false);
    });
  }

  function buildPages(pdf, generation) {
    var n = pdf.numPages;
    var first = [];
    for (var i = 1; i <= Math.min(n, 60); i++) { first.push(pdf.getPage(i)); }
    return Promise.all(first).then(function (loaded) {
      if (generation !== state.generation) { throw new Error("replaced"); }
      var base = loaded[0].getViewport({ scale: 1 });
      var frag = document.createDocumentFragment();
      for (var k = 1; k <= n; k++) {
        var page = loaded[k - 1] || null;
        var vp = page ? page.getViewport({ scale: 1 }) : base;
        var el = document.createElement("div");
        el.className = "ev-page";
        el.setAttribute("data-page", String(k));
        var overlay = document.createElement("div");
        overlay.className = "ev-overlay";
        el.appendChild(overlay);
        frag.appendChild(el);
        state.pages.push({ n: k, el: el, overlay: overlay, page: page, w: vp.width, h: vp.height,
                           rendered: 0, task: null, canvas: null, visible: false });
      }
      scroller.appendChild(frag);
      countEl.textContent = String(n);
      currentEl.textContent = "1";
      scroller.scrollTop = 0;
      scroller.scrollLeft = 0;
      layout();
      observer = new IntersectionObserver(onIntersect, { root: scroller, rootMargin: "600px 0px" });
      state.pages.forEach(function (p) { observer.observe(p.el); });
    });
  }

  function fitScale() {
    var widest = 0;
    state.pages.forEach(function (p) { widest = Math.max(widest, p.w); });
    var avail = Math.max(120, scroller.clientWidth - 2 * PAD);
    return widest ? avail / widest : 1;
  }

  /* Size every page placeholder for the current zoom (keeps the reading position), then re-render. */
  function layout() {
    if (!state.pages.length) { return; }
    var anchor = readingAnchor();
    state.scale = fitScale() * state.zoom;
    state.pages.forEach(function (p) {
      p.el.style.width = Math.floor(p.w * state.scale) + "px";
      p.el.style.height = Math.floor(p.h * state.scale) + "px";
    });
    if (anchor) {
      var a = state.pages[anchor.page - 1];
      scroller.scrollTop = a.el.offsetTop + anchor.frac * a.el.offsetHeight - PAD;
    }
    zoomLabel.textContent = state.zoom === 1 ? "Fit width" : Math.round(state.zoom * 100) + "%";
    state.pages.forEach(function (p) { if (p.visible) { render(p); } });
  }

  function readingAnchor() {
    var top = scroller.scrollTop + PAD;
    for (var i = 0; i < state.pages.length; i++) {
      var el = state.pages[i].el;
      if (el.offsetTop + el.offsetHeight > top && el.offsetHeight) {
        return { page: i + 1, frac: Math.max(0, (top - el.offsetTop) / el.offsetHeight) };
      }
    }
    return null;
  }

  var MAX_CANVASES = 10;   // long documents: release pages far from view
  function onIntersect(entries) {
    entries.forEach(function (en) {
      var p = state.pages[Number(en.target.getAttribute("data-page")) - 1];
      if (!p) { return; }
      p.visible = en.isIntersecting;
      if (p.visible) {
        render(p);
      } else if (p.canvas && state.pages.filter(function (x) { return x.canvas; }).length > MAX_CANVASES) {
        p.canvas.remove();
        p.canvas = null;
        p.rendered = 0;
      }
    });
  }

  function render(p) {
    var generation = state.generation;
    if (!p.page) {
      state.pdf.getPage(p.n).then(function (page) {
        if (generation !== state.generation) { return; }
        p.page = page;
        var vp = page.getViewport({ scale: 1 });
        if (vp.width !== p.w || vp.height !== p.h) { p.w = vp.width; p.h = vp.height; layout(); }
        render(p);
      });
      return;
    }
    var scale = state.scale;
    if (p.rendered === scale) { return; }
    if (p.task) {
      if (p.taskScale === scale) { return; }
      p.task.cancel();
    }
    var vp = p.page.getViewport({ scale: scale });
    // Crisp on HiDPI screens; capped so very large zooms stay under browser canvas limits.
    var ratio = Math.min(window.devicePixelRatio || 1, 3);
    var maxPixels = 16000000;
    if (vp.width * vp.height * ratio * ratio > maxPixels) { ratio = Math.sqrt(maxPixels / (vp.width * vp.height)); }
    var canvas = document.createElement("canvas");
    canvas.className = "ev-canvas";
    canvas.width = Math.max(1, Math.floor(vp.width * ratio));
    canvas.height = Math.max(1, Math.floor(vp.height * ratio));
    canvas.setAttribute("aria-hidden", "true");
    var task = p.page.render({
      canvasContext: canvas.getContext("2d", { alpha: false }), canvas: canvas, viewport: vp,
      transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null
    });
    p.task = task;
    p.taskScale = scale;
    task.promise.then(function () {
      if (p.task !== task) { return; }
      p.task = null;
      if (generation !== state.generation) { return; }
      if (p.canvas) { p.canvas.remove(); }
      p.el.insertBefore(canvas, p.overlay);
      p.canvas = canvas;
      p.rendered = scale;
    }, function (err) {
      if (p.task === task) { p.task = null; }
      if (err && err.name !== "RenderingCancelledException" && window.console) { console.warn("Page render failed", err); }
    });
  }

  // ------------------------------------------------------------------ highlights

  function clearBoxes(kind) {
    scroller.querySelectorAll(".ev-box." + (kind === "preview" ? "is-preview" : "is-active")).forEach(function (b) {
      b.remove();
    });
  }

  function drawBoxes(hits, kind) {
    clearBoxes(kind);
    var made = [];
    hits.forEach(function (hit) {
      var p = state.pages[hit.p - 1];
      if (!p) { return; }
      hit.b.forEach(function (box) {
        var el = document.createElement("div");
        el.className = "ev-box " + (kind === "preview" ? "is-preview" : "is-active");
        el.style.left = (box[0] * 100) + "%";
        el.style.top = (box[1] * 100) + "%";
        el.style.width = (Math.max(box[2] - box[0], 0.004) * 100) + "%";
        el.style.height = (Math.max(box[3] - box[1], 0.004) * 100) + "%";
        p.overlay.appendChild(el);
        made.push(el);
      });
    });
    return made;
  }

  function scrollToHit(hit) {
    var p = state.pages[hit.p - 1];
    if (!p) { return; }
    var box = hit.b[0];
    var w = p.el.offsetWidth, h = p.el.offsetHeight;
    var top = p.el.offsetTop + box[1] * h - scroller.clientHeight * 0.3;
    var left = scroller.scrollLeft;
    var bx0 = p.el.offsetLeft + box[0] * w, bx1 = p.el.offsetLeft + box[2] * w;
    if (bx0 < scroller.scrollLeft || bx1 > scroller.scrollLeft + scroller.clientWidth) {
      left = (bx0 + bx1) / 2 - scroller.clientWidth / 2;
    }
    var opts = { top: Math.max(0, top), left: Math.max(0, left), behavior: reduceMotion ? "auto" : "smooth" };
    if (scroller.scrollTo) { scroller.scrollTo(opts); } else { scroller.scrollTop = opts.top; scroller.scrollLeft = opts.left; }
  }

  function pulse(els) {
    if (reduceMotion) { return; }
    els.forEach(function (el) {
      el.classList.remove("is-pulse");
      void el.offsetWidth;  // restart the animation
      el.classList.add("is-pulse");
    });
  }

  /* What a target string points at: "total_amount", "container_numbers=MSCU1234565",
     "line_items=2" (one line) or "line_items.amount" (every line amount). */
  function resolve(d, target) {
    var name = target, key = null, part = null;
    var eq = target.indexOf("=");
    var dot = target.indexOf(".");
    if (eq > 0) { name = target.slice(0, eq); key = target.slice(eq + 1); }
    else if (dot > 0) { name = target.slice(0, dot); part = target.slice(dot + 1); }
    var f = d.fields[name];
    if (!f) { return { hits: [], reason: MSG.unknown }; }
    var hits = [];
    if (key !== null) {
      if (f.items && f.items[key]) { hits = [f.items[key]]; }
    } else if (f.items) {
      Object.keys(f.items).sort(function (a, b) { return (Number(a) - Number(b)) || (a < b ? -1 : 1); })
        .forEach(function (k) {
          var it = f.items[k];
          if (part === "amount") { if (it.a && it.a.length) { hits.push({ p: it.p, b: it.a }); } }
          else { hits.push(it); }
        });
    } else if (f.hits) {
      hits = f.hits;
    }
    return { hits: hits, reason: hits.length ? null : reasonFor(f) };
  }

  function reasonFor(f) {
    if (f.status === "scanned") { return MSG.scanned; }
    if (f.status === "not_found" || f.status === "partial") { return f.human ? MSG.typed : MSG.notFound; }
    return MSG.unknown;
  }

  function pagesText(hits) {
    var pages = [];
    hits.forEach(function (h) { if (pages.indexOf(h.p) < 0) { pages.push(h.p); } });
    pages.sort(function (a, b) { return a - b; });
    if (pages.length === 1) { return "page " + pages[0]; }
    return "pages " + pages.slice(0, -1).join(", ") + " and " + pages[pages.length - 1];
  }

  function setStatus(text, notice) {
    statusEl.textContent = text;
    statusEl.classList.toggle("is-notice", !!notice);
  }

  function announce(text) {
    liveEl.textContent = "";
    window.setTimeout(function () { liveEl.textContent = text; }, 30);
  }

  /* Highlight targets of one document. opts: {label, announce, scroll, preview} */
  function highlight(d, targets, opts) {
    opts = opts || {};
    if (opts.preview) {
      if (state.doc !== d || !state.pdf) { return; }
      var phits = [];
      targets.forEach(function (t) { phits = phits.concat(resolve(d, t).hits); });
      drawBoxes(phits, "preview");
      return;
    }
    showDoc(d).then(function (ok) {
      if (state.doc !== d) { return; }
      var hits = [], reason = null;
      targets.forEach(function (t) {
        var r = resolve(d, t);
        hits = hits.concat(r.hits);
        reason = reason || r.reason;
      });
      if (!ok) {
        var why = d.mode === "pdfjs" ? MSG.failed : MSG.scanned;
        setStatus(why, true);
        if (opts.announce) { announce(why); }
        return;
      }
      clearBoxes("preview");
      if (!hits.length) {
        clearBoxes("active");
        setStatus(reason || MSG.unknown, true);
        if (opts.announce) { announce(reason || MSG.unknown); }
        return;
      }
      var els = drawBoxes(hits, "active");
      scrollToHit(hits[0]);
      pulse(els);
      var text = "Highlighted " + opts.label + " on " + pagesText(hits) + ".";
      setStatus(text, false);
      if (opts.announce) { announce(text); }
      if (opts.reveal && window.matchMedia("(max-width: 1180px)").matches) {
        aside.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      }
    });
  }

  // ------------------------------------------------------------------ field rows, line rows, issues

  var SVG_NS = "http://www.w3.org/2000/svg";
  function crosshair() {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "i");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    [["circle", { cx: "12", cy: "12", r: "6.5" }], ["path", { d: "M12 2.5v4M12 17.5v4M2.5 12h4M17.5 12h4" }]]
      .forEach(function (spec) {
        var el = document.createElementNS(SVG_NS, spec[0]);
        Object.keys(spec[1]).forEach(function (k) { el.setAttribute(k, spec[1][k]); });
        svg.appendChild(el);
      });
    return svg;
  }

  function rowInfo(row) {
    var d = docFor(row);
    if (!d) { return null; }
    var name = row.getAttribute("data-evidence-field");
    if (name) {
      var f = d.fields[name];
      if (!f) { return null; }  // no value: nothing to show
      return { doc: d, targets: [name], label: f.label };
    }
    var line = row.getAttribute("data-evidence-line");
    if (line !== null) {
      if (!d.fields.line_items) { return null; }
      return { doc: d, targets: ["line_items=" + line], label: "line " + (Number(line) + 1) };
    }
    return null;
  }

  var activeRow = null;
  function activateRow(row, info, opts) {
    if (activeRow && activeRow !== row) { activeRow.classList.remove("ev-row-on"); }
    activeRow = row;
    row.classList.add("ev-row-on");
    highlight(info.doc, info.targets, { label: info.label, announce: opts.announce, reveal: opts.reveal });
  }

  function enhanceRows() {
    document.querySelectorAll("tr[data-evidence-field], tr[data-evidence-line]").forEach(function (row) {
      var info = rowInfo(row);
      if (!info) { return; }
      row.classList.add("ev-row");
      var cell = row.querySelector("td");
      var r = resolve(info.doc, info.targets[0]);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ev-locate";
      btn.appendChild(crosshair());
      if (info.doc.mode === "pdfjs" && r.hits.length) {
        btn.setAttribute("aria-label", "Show " + info.label + " on " + pagesText(r.hits));
        btn.title = "Show on " + pagesText(r.hits);
      } else {
        var why = info.doc.mode === "pdfjs" ? r.reason : MSG.scanned;
        btn.setAttribute("aria-label", "Show " + info.label + " on the page. " + why);
        btn.setAttribute("aria-disabled", "true");
        btn.classList.add("is-off");
        btn.title = why;
      }
      cell.appendChild(btn);

      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        activateRow(row, info, { announce: true, reveal: true });
      });
      row.addEventListener("click", function (e) {
        if (e.target.closest("input, select, textarea, button, a, label, summary")) { return; }
        activateRow(row, info, { announce: true, reveal: true });
      });
      row.addEventListener("focusin", function (e) {
        if (e.target.matches("input:not([type=hidden]), select, textarea")) {
          activateRow(row, info, { announce: false, reveal: false });
        }
      });
      row.addEventListener("mouseenter", function () {
        if (row !== activeRow) { highlight(info.doc, info.targets, { preview: true }); }
      });
      row.addEventListener("mouseleave", function () { clearBoxes("preview"); });
    });
  }

  function enhanceIssues() {
    document.querySelectorAll("[data-evidence-issue]").forEach(function (btn) {
      var d = docs[btn.getAttribute("data-evidence-doc")];
      if (!d) { return; }
      var targets = (btn.getAttribute("data-evidence-targets") || "").split(/\s+/).filter(Boolean);
      var label = btn.getAttribute("data-evidence-label") || "the value";
      btn.hidden = false;
      var any = targets.some(function (t) { return resolve(d, t).hits.length; });
      if (d.mode !== "pdfjs" || !any) {
        btn.setAttribute("aria-disabled", "true");
        btn.classList.add("is-off");
        btn.title = d.mode !== "pdfjs" ? MSG.scanned : resolve(d, targets[0]).reason || MSG.unknown;
      }
      btn.addEventListener("click", function () {
        if (activeRow) { activeRow.classList.remove("ev-row-on"); activeRow = null; }
        highlight(d, targets, { label: label, announce: true, reveal: true });
      });
    });
  }

  // ------------------------------------------------------------------ toolbar, scrolling, resizing

  function setZoom(next) {
    state.zoom = next;
    layout();
  }

  viewer.addEventListener("click", function (e) {
    var z = e.target.closest("[data-ev-zoom]");
    if (z && state.pdf) {
      var mode = z.getAttribute("data-ev-zoom");
      var i = ZOOMS.indexOf(state.zoom);
      if (mode === "fit") { setZoom(1); }
      else if (mode === "in" && i < ZOOMS.length - 1) { setZoom(ZOOMS[i + 1]); }
      else if (mode === "out" && i > 0) { setZoom(ZOOMS[i - 1]); }
      return;
    }
    var pg = e.target.closest("[data-ev-page]");
    if (pg && state.pdf) {
      var cur = Number(currentEl.textContent) || 1;
      var target = pg.getAttribute("data-ev-page") === "next" ? cur + 1 : cur - 1;
      var p = state.pages[Math.max(1, Math.min(state.pages.length, target)) - 1];
      if (p) {
        scroller.scrollTo({ top: p.el.offsetTop - PAD, behavior: reduceMotion ? "auto" : "smooth" });
      }
    }
  });

  var scrollTick = false;
  scroller.addEventListener("scroll", function () {
    if (scrollTick) { return; }
    scrollTick = true;
    window.requestAnimationFrame(function () {
      scrollTick = false;
      var mid = scroller.scrollTop + scroller.clientHeight / 3;
      var cur = 1;
      state.pages.forEach(function (p) { if (p.el.offsetTop <= mid) { cur = p.n; } });
      currentEl.textContent = String(cur);
    });
  });

  // Keyboard zoom inside the pane: + and - (Ctrl/Cmd zoom stays with the browser).
  scroller.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey || !state.pdf) { return; }
    var i = ZOOMS.indexOf(state.zoom);
    if ((e.key === "+" || e.key === "=") && i < ZOOMS.length - 1) { e.preventDefault(); setZoom(ZOOMS[i + 1]); }
    if ((e.key === "-" || e.key === "_") && i > 0) { e.preventDefault(); setZoom(ZOOMS[i - 1]); }
  });

  if (window.ResizeObserver) {
    var lastWidth = 0, resizeTimer = null;
    new ResizeObserver(function () {
      if (Math.abs(scroller.clientWidth - lastWidth) < 2) { return; }
      lastWidth = scroller.clientWidth;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(layout, 120);
    }).observe(scroller);
  }

  // "Show PDF" buttons (app.js updates the title, the Open link and the active card; this shows the document).
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-pdf-url]") : null;
    if (!btn || unavailable) { return; }
    var d = docByUrl(btn.getAttribute("data-pdf-url"));
    if (d) { showDoc(d); }
  });

  // ------------------------------------------------------------------ start

  function initialDoc() {
    var hash = window.location.hash || "";
    if (/^#doc-\d+$/.test(hash) && docs[hash.slice(5)]) { return docs[hash.slice(5)]; }
    var active = document.querySelector(".doc-card.active");
    var d = active ? docFor(active) : null;
    return d || docs[order[0]];
  }

  var first = initialDoc();
  aside.insertBefore(viewer, frame || null);   // the toolbar sits above the pages (or above the iframe)
  viewer.hidden = false;
  if (first.mode === "pdfjs") {
    usePdfjs();  // stop the browser viewer early so it doesn't flash before the pages render
    setStatus("Opening " + first.title + "...", false);
  }
  enhanceRows();
  enhanceIssues();

  // Open the first document when the viewer is (nearly) on screen: at once beside the fields on wide screens,
  // on scrolling down where the layout is stacked. Clicking a value or "Show PDF" first opens that one instead.
  function openFirst() { if (!state.doc) { showDoc(first); } }
  var NEAR = 400;
  if (aside.getBoundingClientRect().top < window.innerHeight + NEAR) {
    openFirst();
  } else {
    var near = new IntersectionObserver(function (entries) {
      if (entries.some(function (en) { return en.isIntersecting; })) { near.disconnect(); openFirst(); }
    }, { rootMargin: NEAR + "px 0px" });
    near.observe(aside);
  }
})();
