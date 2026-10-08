/* Rates pages: add charge lines to a quote, and suggest the unit for an approved extra charge.
   Everything works without this file; it only saves round trips. */
(function () {
  "use strict";

  // Quote form: "Add a line" copies the empty form template and bumps the formset's TOTAL_FORMS.
  var addBtn = document.querySelector("[data-add-charge]");
  var rows = document.querySelector("[data-charge-rows]");
  var tpl = document.getElementById("charge-row-template");
  var total = document.querySelector("input[name$='-TOTAL_FORMS']");
  var maxForms = document.querySelector("input[name$='-MAX_NUM_FORMS']");
  if (addBtn && rows && tpl && total) {
    addBtn.hidden = false;
    var hint = document.querySelector("[data-no-js-hint]");
    if (hint) { hint.hidden = true; }
    addBtn.addEventListener("click", function () {
      var n = parseInt(total.value, 10) || 0;
      if (maxForms && n >= parseInt(maxForms.value, 10)) { return; }
      var html = tpl.innerHTML.replace(/__prefix__/g, String(n));
      var holder = document.createElement("tbody");
      holder.innerHTML = html.trim();
      var row = holder.firstElementChild;
      rows.appendChild(row);
      total.value = String(n + 1);
      var first = row.querySelector("select, input:not([type=hidden])");
      if (first) { first.focus(); }
    });
  }

  // Approved extra charge form: picking a charge suggests its usual unit (days, hours or each).
  var unitsEl = document.getElementById("accessorial-units");
  var code = document.getElementById("id_code");
  var unit = document.getElementById("id_unit");
  if (unitsEl && code && unit) {
    var units = {};
    try { units = JSON.parse(unitsEl.textContent); } catch (e) { units = {}; }
    var touched = false;
    unit.addEventListener("change", function () { touched = true; });
    code.addEventListener("change", function () {
      if (!touched && units[code.value]) { unit.value = units[code.value]; }
    });
  }
})();
