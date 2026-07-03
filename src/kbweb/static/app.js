/* Knowledge Vault — progressive enhancement only; every page works with JS off.
   1. Theme toggle: cycles auto → light → dark, persisted in localStorage
      (base.html applies the stored value pre-paint to avoid a flash).
   2. Copy buttons on highlighted code blocks (pre.hl). */

(function () {
  "use strict";

  var LABELS = { auto: "◐ auto", light: "☀ light", dark: "☾ dark" };

  function currentTheme() {
    return document.documentElement.dataset.theme || "auto";
  }

  function applyTheme(mode) {
    if (mode === "auto") {
      delete document.documentElement.dataset.theme;
      try { localStorage.removeItem("kb-theme"); } catch (e) { /* private mode */ }
    } else {
      document.documentElement.dataset.theme = mode;
      try { localStorage.setItem("kb-theme", mode); } catch (e) { /* private mode */ }
    }
  }

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.textContent = LABELS[currentTheme()];
    toggle.addEventListener("click", function () {
      var order = ["auto", "light", "dark"];
      var next = order[(order.indexOf(currentTheme()) + 1) % order.length];
      applyTheme(next);
      toggle.textContent = LABELS[next];
    });
  }

  document.querySelectorAll("pre.hl").forEach(function (pre) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "copy";
    btn.addEventListener("click", function () {
      var code = pre.querySelector("code") || pre;
      navigator.clipboard.writeText(code.innerText).then(function () {
        btn.textContent = "copied";
        setTimeout(function () { btn.textContent = "copy"; }, 1200);
      });
    });
    pre.appendChild(btn);
  });
})();
