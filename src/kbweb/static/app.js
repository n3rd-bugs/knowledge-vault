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

  /* 3. Setup page: reveal/hide + copy the bearer token (kept masked by default).
     The value lives in data-secret so the plaintext only appears on demand. */
  var tok = document.getElementById("kb-token");
  if (tok) {
    var secret = tok.getAttribute("data-secret") || "";
    var masked = tok.textContent;
    var reveal = document.getElementById("token-reveal");
    var copy = document.getElementById("token-copy");
    if (reveal) reveal.addEventListener("click", function () {
      var shown = tok.textContent === secret;
      tok.textContent = shown ? masked : secret;
      reveal.textContent = shown ? "reveal" : "hide";
    });
    if (copy) copy.addEventListener("click", function () {
      navigator.clipboard.writeText(secret).then(function () {
        copy.textContent = "copied";
        setTimeout(function () { copy.textContent = "copy"; }, 1200);
      });
    });
  }

  /* 4. Setup page: "run consolidation now" — POST to the write-capable REST API
     (cross-origin, bearer from the token box), then poll status until terminal.
     Keeps the web UI itself read-only: the write happens in the REST service. */
  var cbtn = document.getElementById("consolidate-btn");
  if (cbtn && tok) {
    var restBase = cbtn.getAttribute("data-rest");
    var statusEl = document.getElementById("consolidate-status");
    var tokSecret = tok.getAttribute("data-secret") || "";
    var timer = null;
    function auth() { return { Authorization: "Bearer " + tokSecret }; }
    function show(st) {
      if (!st) { statusEl.textContent = "no run recorded"; return null; }
      statusEl.textContent = "run #" + st.id + " (" + st.requested_by + "): " + st.status;
      return st.status;
    }
    function poll() {
      fetch(restBase + "/api/consolidate", { headers: auth() })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var s = show(d.status);
          if (s === "done" || s === "failed") {
            clearInterval(timer); timer = null; cbtn.disabled = false;
          }
        })
        .catch(function () { /* transient — keep polling */ });
    }
    cbtn.addEventListener("click", function () {
      cbtn.disabled = true;
      statusEl.textContent = "requesting…";
      fetch(restBase + "/api/consolidate", { method: "POST", headers: auth() })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          show(d.status);
          if (!timer) timer = setInterval(poll, 5000);
        })
        .catch(function (e) {
          statusEl.textContent = "error: could not reach the REST API (" + e + ")";
          cbtn.disabled = false;
        });
    });
  }
})();
