/* Email intake settings: small conveniences on the mailbox forms (CSP: no inline scripts). */
(function () {
  "use strict";

  // IMAP: switching security moves the port between the usual 993 (SSL/TLS) and 143 (STARTTLS).
  document.querySelectorAll("select[data-port-target]").forEach(function (select) {
    var port = document.querySelector(select.getAttribute("data-port-target"));
    if (!port) { return; }
    var usual = { ssl: "993", starttls: "143" };
    select.addEventListener("change", function () {
      if (!port.value || port.value === "993" || port.value === "143") {
        port.value = usual[select.value] || port.value;
      }
    });
  });

  // Microsoft 365: show "Move it to" only when the chosen action moves the email.
  document.querySelectorAll("select[data-move-toggle]").forEach(function (select) {
    var target = document.querySelector(select.getAttribute("data-move-toggle"));
    if (!target) { return; }
    function update() {
      var moves = select.value === "move" || select.value === "category_move";
      target.hidden = !moves;
    }
    select.addEventListener("change", update);
    update();
  });
})();
