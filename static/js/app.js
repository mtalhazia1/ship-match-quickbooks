/* ShipMatch page behaviour. Loaded on every page; no inline scripts are used (CSP: script-src 'self'). */
(function () {
  "use strict";

  // Time zone: tell the server the browser's zone so dates show in local time.
  try {
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    var current = (document.cookie.match(/(?:^|; )tz=([^;]*)/) || [])[1];
    current = current ? decodeURIComponent(current) : "";
    if (tz && current !== tz) {
      document.cookie = "tz=" + encodeURIComponent(tz) + "; path=/; max-age=31536000; samesite=lax";
      var source = document.body.getAttribute("data-tz-source");
      var serverTz = document.body.getAttribute("data-tz");
      if ((source === "default" || source === "organization") && serverTz !== tz && !current) {
        window.location.reload();
        return;
      }
    }
  } catch (e) { /* older browsers: server falls back to the organization's zone */ }

  // Submit a form when a field inside it changes (field edits, filters, org switcher).
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("[data-autosubmit]")) {
      var form = el.form || el.closest("form");
      if (form) {
        if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
      }
    }
  });

  // Field edits: Enter saves, Escape restores the original value.
  document.addEventListener("keydown", function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches("input[data-autosubmit]")) { return; }
    if (e.key === "Escape") { el.value = el.defaultValue; el.blur(); }
  });

  // PDF viewer: buttons with data-pdf-url load that document into the side viewer.
  var viewer = document.getElementById("pdf-frame");
  var viewerTitle = document.getElementById("pdf-title");
  var viewerOpen = document.getElementById("pdf-open");
  function showPdf(btn) {
    if (!viewer) { window.open(btn.getAttribute("data-pdf-url"), "_blank", "noopener"); return; }
    viewer.src = btn.getAttribute("data-pdf-url");
    if (viewerTitle) { viewerTitle.textContent = btn.getAttribute("data-pdf-title") || ""; }
    if (viewerOpen) { viewerOpen.href = btn.getAttribute("data-pdf-url"); }
    document.querySelectorAll(".doc-card.active").forEach(function (c) { c.classList.remove("active"); });
    var card = btn.closest(".doc-card");
    if (card) { card.classList.add("active"); }
    if (window.matchMedia("(max-width: 1180px)").matches) {
      // The iframe is detached while the PDF.js viewer is on, so scroll to the whole viewer panel instead.
      var panel = viewer.isConnected ? viewer : document.querySelector(".viewer");
      if (panel) { panel.scrollIntoView({ behavior: "smooth", block: "start" }); }
    }
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-pdf-url]") : null;
    if (btn) { e.preventDefault(); showPdf(btn); }
  });

  // Copy buttons: data-copy="#element-id" copies that element's text.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-copy]") : null;
    if (!btn) { return; }
    var src = document.querySelector(btn.getAttribute("data-copy"));
    if (!src || !navigator.clipboard) { return; }
    navigator.clipboard.writeText(src.textContent.trim()).then(function () {
      var label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = label; }, 1600);
    });
  });

  // Mobile navigation.
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-nav-toggle]")) {
      document.body.classList.toggle("nav-open");
    } else if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar")) {
      document.body.classList.remove("nav-open");
    }
  });

  // Close open menus when clicking elsewhere or pressing Escape.
  function closeMenus(except) {
    document.querySelectorAll("details[data-menu][open]").forEach(function (d) { if (d !== except) { d.open = false; } });
  }
  document.addEventListener("click", function (e) {
    closeMenus(e.target.closest ? e.target.closest("details[data-menu]") : null);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeMenus(null); document.body.classList.remove("nav-open"); }
    // "/" focuses search unless typing in a field.
    if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });

  // Upload drop zone: drag PDFs onto it, show chosen file names.
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var status = zone.querySelector("[data-file-status]");
    function update() {
      if (!status || !input) { return; }
      var n = input.files.length;
      status.textContent = n === 0 ? "No files chosen" : (n === 1 ? input.files[0].name : n + " files chosen");
    }
    if (input) { input.addEventListener("change", update); }
    ["dragenter", "dragover"].forEach(function (t) {
      zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.remove("drag"); });
    });
    zone.addEventListener("drop", function (e) {
      if (input && e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        update();
      }
    });
  });

  // Prevent double submits on forms that change state.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.method && form.method.toLowerCase() === "post") {
      if (form.dataset.submitting === "1") { e.preventDefault(); return; }
      form.dataset.submitting = "1";
      setTimeout(function () { form.dataset.submitting = ""; }, 4000);
    }
  });

  // Focus the first invalid or autofocus field inside an opened <details> form.
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d.tagName === "DETAILS" && d.open) {
      var f = d.querySelector("textarea, input:not([type=hidden])");
      if (f && d.hasAttribute("data-focus")) { f.focus(); }
    }
  }, true);
})();
