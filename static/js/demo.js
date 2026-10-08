/* Demo mode: "Use this account" buttons on the sign-in page fill in the username and password. */
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-demo-username]") : null;
    if (!btn) { return; }
    e.preventDefault();
    var user = document.getElementById("id_username");
    var pass = document.getElementById("id_password");
    if (!user || !pass) { return; }
    user.value = btn.getAttribute("data-demo-username") || "";
    pass.value = btn.getAttribute("data-demo-password") || "";
    document.querySelectorAll("[data-demo-username]").forEach(function (b) {
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    var submit = user.form ? user.form.querySelector("button[type=submit]") : null;
    if (submit) { submit.focus(); }
  });
})();
