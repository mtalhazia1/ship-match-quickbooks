/* ROI calculator: live results while typing. Mirrors apps/rates/roi.py exactly (same formulas,
   limits, rounding and messages); the page also works without this file (the form submits). */
(function () {
  "use strict";

  var form = document.querySelector("form[data-roi]");
  if (!form) { return; }

  var FIELDS = ["invoices", "minutes_today", "minutes_with", "hourly_cost", "error_share", "avg_overcharge", "price"];

  function round(x, places) {
    var f = Math.pow(10, places);
    var sign = x < 0 ? -1 : 1;
    return sign * Math.round(Math.abs(x) * f + 1e-9) / f;
  }

  function fmtMoney(cur, x) {
    return cur + " " + round(x, 0).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function fmtOne(x) {
    return round(x, 1).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function labelFor(input) {
    var l = form.querySelector("label[for='" + input.id + "']");
    return l ? l.textContent.trim().toLowerCase() : input.name;
  }

  // Same rules as roi.calculate(): blank = example value; numbers within min..max; whole invoices.
  function read() {
    var values = {}, errors = {};
    FIELDS.forEach(function (name) {
      var input = form.elements[name];
      if (!input) { return; }
      var raw = String(input.value || "").trim().replace(/,/g, "");
      if (raw === "") { values[name] = parseFloat(input.getAttribute("data-default")); return; }
      var v = Number(raw);
      if (!isFinite(v) || !/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(raw)) {
        errors[name] = "Enter a number for “" + labelFor(input) + "”.";
        return;
      }
      if (name === "invoices" && Math.floor(v) !== v) { errors[name] = "Enter a whole number of invoices."; return; }
      var lo = Number(input.min), hi = Number(input.max);
      if (v < lo || v > hi) {
        errors[name] = "Enter a value from " + lo.toLocaleString("en-US") + " to " + hi.toLocaleString("en-US") + ".";
        return;
      }
      values[name] = v;
    });
    if (!errors.minutes_today && !errors.minutes_with && values.minutes_with > values.minutes_today) {
      errors.minutes_with = "Minutes with ShipMatch can't be more than minutes today.";
    }
    return { values: values, errors: errors };
  }

  function compute(v) {
    var hours = v.invoices * (v.minutes_today - v.minutes_with) / 60;
    var labor = hours * 12 * v.hourly_cost;
    var over = v.invoices * 12 * v.error_share / 100 * v.avg_overcharge;
    var cost = v.price * 12;
    var grossMonth = (labor + over) / 12;
    return {
      hours: round(hours, 1), labor: round(labor, 0), over: round(over, 0), cost: round(cost, 0),
      net: round(labor + over - cost, 0), gross: round(labor, 0) + round(over, 0),
      payback: grossMonth > 0 ? round(cost / grossMonth, 1) : null
    };
  }

  function setOut(name, text) {
    form.ownerDocument.querySelectorAll("[data-out='" + name + "']").forEach(function (el) { el.textContent = text; });
  }

  function showErrors(errors) {
    FIELDS.forEach(function (name) {
      var list = form.querySelector("[data-error-for='" + name + "']");
      var field = list ? list.closest(".field") : null;
      if (!list) { return; }
      list.innerHTML = "";
      if (errors[name]) {
        var li = document.createElement("li");
        li.textContent = errors[name];
        list.appendChild(li);
      }
      if (field) { field.classList.toggle("has-error", Boolean(errors[name])); }
    });
  }

  function shareUrl() {
    var params = new URLSearchParams();
    params.set("currency", form.elements.currency.value);
    FIELDS.forEach(function (name) { if (form.elements[name]) { params.set(name, form.elements[name].value); } });
    return window.location.origin + window.location.pathname + "?" + params.toString();
  }

  function update() {
    var cur = form.elements.currency ? form.elements.currency.value : "USD";
    document.querySelectorAll("[data-currency-label]").forEach(function (el) { el.textContent = cur; });
    var state = read();
    showErrors(state.errors);
    var net = document.querySelector("[data-out='net_year']");
    if (Object.keys(state.errors).length) {
      ["net_year", "payback", "hours_saved_month", "labor_saved_year", "overcharges_year", "cost_year", "gross_year"]
        .forEach(function (n) { setOut(n, "–"); });
      setOut("payback_text", "Fix the highlighted numbers to see results.");
      if (net) { net.classList.remove("negative"); }
      return;
    }
    var r = compute(state.values);
    setOut("net_year", fmtMoney(cur, r.net));
    setOut("hours_saved_month", fmtOne(r.hours));
    setOut("labor_saved_year", fmtMoney(cur, r.labor));
    setOut("overcharges_year", fmtMoney(cur, r.over));
    setOut("cost_year", fmtMoney(cur, r.cost));
    setOut("gross_year", fmtMoney(cur, r.gross));
    if (r.payback === null) {
      setOut("payback", "Not reached");
      setOut("payback_text", "At these numbers the savings don't cover the cost.");
    } else {
      setOut("payback", fmtOne(r.payback) + " months");
      setOut("payback_text", "Savings pay for a year of ShipMatch in " + fmtOne(r.payback) + " months.");
    }
    if (net) { net.classList.toggle("negative", r.net < 0); }
    var url = shareUrl();
    var link = document.querySelector("[data-share-url]");
    if (link) { link.textContent = url; }
    try { window.history.replaceState(null, "", url); } catch (e) { /* file:// or sandboxed */ }
  }

  form.addEventListener("input", update);
  form.addEventListener("change", update);

  // Exposed for tests and the console: same math as the server.
  window.ShipMatchROI = { compute: compute, round: round, fmtMoney: fmtMoney, fmtOne: fmtOne };
})();
