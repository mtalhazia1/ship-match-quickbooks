/* Team workflow: keyboard shortcuts, bulk selection in the review queue, confirm dialogs and @mention
   autocomplete. Loaded on every signed-in page (CSP: script-src 'self', no inline scripts). */
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="wf-shortcuts"]');
  var shortcutsOn = !!meta && meta.getAttribute("content") === "on";

  // ------------------------------------------------------------------ helpers
  var live = document.createElement("div");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  live.id = "wf-live";
  document.body.appendChild(live);
  function announce(text) {
    live.textContent = "";
    window.setTimeout(function () { live.textContent = text; }, 30);
  }

  function typing(el) {
    if (!el || el === document.body) { return false; }
    if (el.isContentEditable) { return true; }
    var tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") { return true; }
    if (tag === "INPUT") {
      var t = (el.getAttribute("type") || "text").toLowerCase();
      return ["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image"].indexOf(t) === -1;
    }
    return false;
  }

  function toArray(list) { return Array.prototype.slice.call(list || []); }

  // ------------------------------------------------------------------ dialogs
  var lastOpener = null;
  function openDialog(dialog, opener) {
    if (!dialog) { return; }
    lastOpener = opener || document.activeElement;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) { dialog.showModal(); }
    } else {
      dialog.setAttribute("open", "");
    }
    var focus = dialog.querySelector("[data-wf-autofocus]") || dialog.querySelector("button, [href], input, select, textarea");
    if (focus) { focus.focus(); }
  }
  function closeDialog(dialog) {
    if (!dialog) { return; }
    if (typeof dialog.close === "function") { dialog.close(); } else { dialog.removeAttribute("open"); }
  }
  toArray(document.querySelectorAll("dialog.wf-dialog")).forEach(function (dialog) {
    dialog.addEventListener("close", function () {
      if (lastOpener && document.contains(lastOpener) && typeof lastOpener.focus === "function") { lastOpener.focus(); }
      lastOpener = null;
    });
    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) { closeDialog(dialog); }   // click on the backdrop
    });
  });
  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target : null;
    if (!t) { return; }
    var closer = t.closest("[data-wf-close]");
    if (closer) { closeDialog(closer.closest("dialog")); return; }
    var help = t.closest("[data-wf-help-open]");
    if (help) { openDialog(document.getElementById("wf-help"), help); return; }
    var approveOpen = t.closest("[data-wf-approve-open]");
    if (approveOpen) { openDialog(document.getElementById("wf-approve-dialog"), approveOpen); return; }
    // A form whose submit button first opens a confirmation dialog (the dialog's own form does the work).
    var submit = t.closest("form[data-wf-confirm] button[type=submit]");
    if (submit) {
      var dialog = document.getElementById(submit.form.getAttribute("data-wf-confirm"));
      if (dialog) { e.preventDefault(); openDialog(dialog, submit); }
    }
  });

  // ------------------------------------------------------------------ bulk selection (review queue)
  var bulkForm = document.getElementById("wf-bulk");
  var selectAll = document.querySelector("[data-wf-select-all]");
  var lastBox = null;
  function boxes() { return toArray(document.querySelectorAll("input[data-wf-check]")); }
  function refreshSelection() {
    var all = boxes();
    var n = all.filter(function (c) { return c.checked; }).length;
    toArray(document.querySelectorAll("[data-wf-selected-count]")).forEach(function (el) { el.textContent = n; });
    toArray(document.querySelectorAll("[data-wf-needs-selection]")).forEach(function (b) { b.disabled = n === 0; });
    if (selectAll) {
      selectAll.checked = n > 0 && n === all.length;
      selectAll.indeterminate = n > 0 && n < all.length;
    }
    all.forEach(function (c) {
      var row = c.closest("tr");
      if (row) { row.classList.toggle("wf-row-checked", c.checked); }
    });
  }
  if (bulkForm) {
    document.addEventListener("click", function (e) {
      var box = e.target.closest ? e.target.closest("input[data-wf-check]") : null;
      if (!box) { return; }
      if (e.shiftKey && lastBox && lastBox !== box) {
        var all = boxes();
        var a = all.indexOf(lastBox), b = all.indexOf(box);
        if (a > -1 && b > -1) {
          var from = Math.min(a, b), to = Math.max(a, b);
          for (var i = from; i <= to; i++) { all[i].checked = box.checked; }
        }
      }
      lastBox = box;
      refreshSelection();
    });
    if (selectAll) {
      selectAll.addEventListener("change", function () {
        boxes().forEach(function (c) { c.checked = selectAll.checked; });
        refreshSelection();
        announce(selectAll.checked ? "All shipments on this page selected" : "Selection cleared");
      });
    }
    bulkForm.addEventListener("submit", function (e) {
      var btn = e.submitter;
      if (btn && btn.value === "assign") {
        var who = bulkForm.querySelector("select[name=assignee]");
        if (who && !who.value) { e.preventDefault(); who.focus(); announce("Choose who to assign the shipments to"); }
      }
    });
    refreshSelection();
  }

  // ------------------------------------------------------------------ rows (j, k, Enter, x)
  function primaryLink(row) { return row.querySelector("a.row-link, .wf-note-link"); }
  function rows() {
    return toArray(document.querySelectorAll("main table.data tbody tr, main .wf-notes > li"))
      .filter(function (r) { return !!primaryLink(r); });
  }
  function currentRow() { return document.querySelector("main .wf-row-on"); }
  function selectRow(row, list) {
    var old = currentRow();
    if (old) { old.classList.remove("wf-row-on"); }
    row.classList.add("wf-row-on");
    var link = primaryLink(row);
    if (link) { link.focus({ preventScroll: true }); }
    row.scrollIntoView({ block: "nearest" });
    announce("Row " + (list.indexOf(row) + 1) + " of " + list.length);
  }
  function moveRow(delta) {
    var list = rows();
    if (!list.length) { return false; }
    var cur = currentRow();
    var focused = document.activeElement && document.activeElement.closest ? document.activeElement.closest("tr, li") : null;
    var i = list.indexOf(cur);
    if (i < 0 && focused) { i = list.indexOf(focused); }
    var next = i < 0 ? (delta > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + delta));
    selectRow(list[next], list);
    return true;
  }
  function moveShipment(delta) {
    var link = document.querySelector(delta > 0 ? "[data-wf-next]" : "[data-wf-prev]");
    if (link) { window.location.href = link.href; return true; }
    if (document.querySelector("[data-wf-next], [data-wf-prev]") || document.getElementById("wf-approve-dialog")) {
      announce(delta > 0 ? "This is the last shipment in its tab" : "This is the first shipment in its tab");
      return true;
    }
    return false;
  }
  function toggleRowBox() {
    var row = currentRow();
    var box = row ? row.querySelector("input[data-wf-check]") : null;
    if (!box) { return false; }
    box.checked = !box.checked;
    lastBox = box;
    refreshSelection();
    announce(box.checked ? "Selected" : "Not selected");
    return true;
  }

  // ------------------------------------------------------------------ actions (a, r, e)
  function approve() {
    var dialog = document.getElementById("wf-approve-dialog");
    if (dialog) { openDialog(dialog, document.activeElement); return true; }
    var btn = document.querySelector("[data-wf-bulk-approve]");
    if (!btn || !bulkForm) { return false; }
    var picked = boxes().filter(function (c) { return c.checked; });
    if (!picked.length) {
      var row = currentRow();
      var box = row ? row.querySelector("input[data-wf-check]") : null;
      if (!box) { announce("Select a shipment first: j and k move, x selects"); return true; }
      box.checked = true;
      refreshSelection();
    }
    // Goes to the confirmation page that shows what each shipment's checks say; nothing is approved yet.
    btn.disabled = false;
    if (bulkForm.requestSubmit) { bulkForm.requestSubmit(btn); } else { btn.click(); }
    return true;
  }
  function reject() {
    var form = document.querySelector('main form[action*="/reject/"]');
    if (!form) { return false; }
    var details = form.closest("details");
    if (details) { details.open = true; }
    var note = form.querySelector("textarea");
    if (note) { note.focus(); }
    form.scrollIntoView({ block: "center" });
    return true;
  }
  var lastField = null;
  document.addEventListener("focusin", function (e) {
    if (e.target.matches && e.target.matches(".fields input[type=text]")) { lastField = e.target; }
  });
  function editField() {
    var pick = document.querySelector("main tr.ev-row-on input[type=text]")
      || (lastField && document.contains(lastField) ? lastField : null)
      || document.querySelector("main .fields tr.f-missing input[type=text]")
      || document.querySelector("main .fields tr.f-low input[type=text]")
      || document.querySelector("main .fields input[type=text]");
    if (!pick) { return false; }
    pick.scrollIntoView({ block: "center" });
    pick.focus();
    if (pick.select) { pick.select(); }
    return true;
  }

  // ------------------------------------------------------------------ the key handler
  var help = document.getElementById("wf-help");
  var goTargets = help ? { q: help.getAttribute("data-wf-go-q"), d: help.getAttribute("data-wf-go-d"),
    s: help.getAttribute("data-wf-go-s") } : {};
  var pendingG = false, gTimer = null;

  document.addEventListener("keydown", function (e) {
    if (!shortcutsOn || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) { return; }
    if (typing(e.target) || typing(document.activeElement)) { return; }
    if (document.querySelector("dialog[open]")) { return; }
    var key = e.key;
    if (pendingG) {
      pendingG = false;
      window.clearTimeout(gTimer);
      var url = goTargets[key];
      if (url) { e.preventDefault(); window.location.href = url; }
      return;
    }
    var handled = false;
    switch (key) {
      case "?":
        openDialog(help, document.activeElement);
        handled = true;
        break;
      case "g":
        pendingG = true;
        gTimer = window.setTimeout(function () { pendingG = false; }, 1500);
        handled = true;
        break;
      case "j":
        handled = moveShipment(1) || moveRow(1);
        break;
      case "k":
        handled = moveShipment(-1) || moveRow(-1);
        break;
      case "Enter":
        var row = currentRow();
        var active = document.activeElement;
        if (row && (!active || active === document.body || !active.closest("a, button, summary, input, select, textarea"))) {
          var link = primaryLink(row);
          if (link) { link.click(); handled = true; }
        }
        break;
      case "x":
        handled = toggleRowBox();
        break;
      case "a":
        handled = approve();
        break;
      case "r":
        handled = reject();
        break;
      case "e":
        handled = editField();
        break;
    }
    if (handled) { e.preventDefault(); }
  });

  // ------------------------------------------------------------------ @mention autocomplete
  var MENTION_BEFORE_CARET = /(^|[^\w@])@([A-Za-z0-9_.+\-@]*)$/;
  var uid = 0;
  function mentions(area) {
    var url = area.getAttribute("data-wf-mentions");
    if (!url) { return; }
    uid += 1;
    var list = document.createElement("ul");
    list.className = "wf-suggest";
    list.id = (area.id || "wf-area-" + uid) + "-people";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "People to mention");
    list.hidden = true;
    var wrap = document.createElement("div");
    wrap.className = "wf-suggest-wrap";
    area.parentNode.insertBefore(wrap, area);
    wrap.appendChild(area);
    wrap.appendChild(list);
    area.setAttribute("aria-autocomplete", "list");
    area.setAttribute("aria-controls", list.id);
    area.setAttribute("aria-expanded", "false");
    var items = [], active = -1, timer = null, seq = 0;

    function token() {
      var pos = area.selectionStart;
      if (pos == null || pos !== area.selectionEnd) { return null; }
      var m = MENTION_BEFORE_CARET.exec(area.value.slice(0, pos));
      return m ? { start: pos - m[2].length - 1, end: pos, query: m[2] } : null;
    }
    function close() {
      list.hidden = true;
      list.innerHTML = "";
      items = [];
      active = -1;
      area.setAttribute("aria-expanded", "false");
      area.removeAttribute("aria-activedescendant");
    }
    function highlight(i) {
      active = i;
      toArray(list.children).forEach(function (li, n) { li.setAttribute("aria-selected", n === i ? "true" : "false"); });
      if (i > -1 && list.children[i]) { area.setAttribute("aria-activedescendant", list.children[i].id); }
    }
    function show(members) {
      items = members || [];
      list.innerHTML = "";
      if (!items.length) { close(); return; }
      items.forEach(function (m, i) {
        var li = document.createElement("li");
        li.id = list.id + "-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("data-i", String(i));
        var name = document.createElement("strong");
        name.textContent = m.name;
        var sub = document.createElement("span");
        sub.textContent = "@" + m.username + (m.role ? ", " + m.role : "");
        li.appendChild(name);
        li.appendChild(sub);
        list.appendChild(li);
      });
      list.hidden = false;
      area.setAttribute("aria-expanded", "true");
      highlight(0);
      announce(items.length + (items.length === 1 ? " person" : " people") + " found. Use the arrow keys and Enter to pick.");
    }
    function pick(i) {
      var t = token();
      var m = items[i];
      if (!t || !m) { close(); return; }
      var insert = "@" + m.username + " ";
      area.value = area.value.slice(0, t.start) + insert + area.value.slice(t.end);
      var caret = t.start + insert.length;
      area.setSelectionRange(caret, caret);
      close();
      area.focus();
    }
    function lookup(q) {
      var mine = ++seq;
      var sep = url.indexOf("?") > -1 ? "&" : "?";
      fetch(url + sep + "q=" + encodeURIComponent(q), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : { members: [] }; })
        .then(function (data) { if (mine === seq && token()) { show(data.members); } })
        .catch(function () { close(); });
    }
    area.addEventListener("input", function () {
      var t = token();
      if (!t) { close(); return; }
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { lookup(t.query); }, 120);
    });
    area.addEventListener("keydown", function (e) {
      if (list.hidden) { return; }
      if (e.key === "ArrowDown") { e.preventDefault(); highlight((active + 1) % items.length); }
      else if (e.key === "ArrowUp") { e.preventDefault(); highlight((active - 1 + items.length) % items.length); }
      else if (e.key === "Enter" || e.key === "Tab") { if (active > -1) { e.preventDefault(); pick(active); } }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    });
    area.addEventListener("blur", function () { window.setTimeout(close, 150); });
    list.addEventListener("mousedown", function (e) {
      var li = e.target.closest ? e.target.closest("li[data-i]") : null;
      if (li) { e.preventDefault(); pick(parseInt(li.getAttribute("data-i"), 10)); }
    });
  }
  toArray(document.querySelectorAll("textarea[data-wf-mentions]")).forEach(mentions);
})();
