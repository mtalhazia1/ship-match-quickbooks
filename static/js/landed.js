/* Shared invoices: typing an amount chooses "Amounts I type" and shows what is left to assign.
   The server checks the split again; this only helps while typing. No inline scripts (CSP). */
(function () {
  "use strict";

  function cents(text) {
    var s = String(text || "").replace(/[\s,]/g, "");
    if (!/^-?\d+(\.\d{1,2})?$/.test(s)) { return null; }
    var parts = s.split(".");
    var whole = parseInt(parts[0], 10);
    var frac = parts[1] ? parseInt((parts[1] + "0").slice(0, 2), 10) : 0;
    return s.charAt(0) === "-" ? whole * 100 - frac : whole * 100 + frac;
  }

  function money(c) {
    var sign = c < 0 ? "-" : "";
    c = Math.abs(c);
    var whole = String(Math.floor(c / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return sign + whole + "." + String(c % 100).padStart(2, "0");
  }

  function update(form) {
    var total = cents(form.getAttribute("data-total"));
    var out = form.querySelector("[data-lc-left]");
    if (total === null || !out) { return; }
    var manual = form.querySelector("input[name=basis][value=manual]");
    if (!manual || !manual.checked) { return; }
    var sum = 0, bad = false;
    form.querySelectorAll("[data-lc-amount]").forEach(function (input) {
      var row = input.closest("tr");
      var removed = row && row.querySelector("input[name=remove]:checked");
      if (removed) { return; }
      var c = cents(input.value);
      if (c === null) { bad = true; input.classList.add("invalid"); } else { input.classList.remove("invalid"); sum += c; }
    });
    var cur = form.getAttribute("data-currency") || "";
    var left = total - sum;
    out.classList.toggle("lc-off", left !== 0 || bad);
    if (bad) { out.textContent = "Type each amount as a number, for example 1240.00."; }
    else if (left === 0) { out.textContent = "The shares add up to the invoice total."; }
    else if (left > 0) { out.textContent = "Still to assign: " + cur + " " + money(left) + "."; }
    else { out.textContent = "Over the invoice total by " + cur + " " + money(-left) + "."; }
  }

  document.querySelectorAll("form[data-lc-split]").forEach(function (form) {
    form.addEventListener("input", function (e) {
      if (e.target.matches("[data-lc-amount]")) {
        var manual = form.querySelector("input[name=basis][value=manual]");
        if (manual) { manual.checked = true; }
      }
      update(form);
    });
    form.addEventListener("change", function () { update(form); });
    update(form);
  });
})();
