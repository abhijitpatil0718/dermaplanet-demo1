/* DermaPlanet front-end behaviour. No inline scripts are used (see the CSP header in app.py). */
document.addEventListener("DOMContentLoaded", function () {
  /* ---- Mobile navigation ---- */
  var toggle = document.querySelector(".nav-toggle");
  var menu = document.getElementById("nav-menu");
  if (toggle && menu) {
    toggle.addEventListener("click", function () {
      var open = menu.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.classList.contains("is-open")) {
        menu.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
      }
    });
  }

  /* ---- Light / dark theme toggle (choice is remembered on this device) ---- */
  var themeBtn = document.querySelector(".theme-toggle");
  var root = document.documentElement;
  function paintToggle() {
    var dark = root.getAttribute("data-theme") === "dark";
    if (themeBtn) {
      themeBtn.setAttribute("aria-pressed", String(dark));
      themeBtn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#0c1813" : "#1a7f5c");
  }
  if (themeBtn) {
    paintToggle();
    themeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("dp-theme", next); } catch (e) {}
      paintToggle();
    });
  }
  /* Follow the device setting until the visitor picks a theme themselves */
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
      var saved = null;
      try { saved = localStorage.getItem("dp-theme"); } catch (err) {}
      if (!saved) { root.setAttribute("data-theme", e.matches ? "dark" : "light"); paintToggle(); }
    });
  }

  /* ---- Dismissable flash messages (success ones fade out on their own) ---- */
  document.querySelectorAll(".flash").forEach(function (flash) {
    var close = flash.querySelector(".flash-close");
    if (close) close.addEventListener("click", function () { flash.remove(); });
    if (flash.classList.contains("flash-success")) {
      setTimeout(function () { flash.remove(); }, 8000);
    }
  });

  /* ---- Confirm before destructive actions ---- */
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-confirm]");
    if (el && !window.confirm(el.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  });

  /* ---- Phone fields: digits only while typing ---- */
  document.querySelectorAll('input[type="tel"]').forEach(function (input) {
    input.addEventListener("input", function () {
      input.value = input.value.replace(/[^\d+\s-]/g, "");
    });
  });

  /* ---- Password confirmation hint ---- */
  document.querySelectorAll("form").forEach(function (form) {
    var pw = form.querySelector('input[name="password"], input[name="new"]');
    var confirm = form.querySelector('input[name="confirm"]');
    if (!pw || !confirm) return;
    function check() {
      confirm.setCustomValidity(confirm.value && confirm.value !== pw.value ? "Passwords do not match" : "");
    }
    pw.addEventListener("input", check);
    confirm.addEventListener("input", check);
  });

  /* ---- Appointment page: load free slots for the chosen doctor and date ---- */
  var form = document.querySelector(".appointment-form");
  var doctor = document.getElementById("doctor");
  var date = document.getElementById("date");
  var box = document.getElementById("slots");
  if (!form || !doctor || !date || !box) return;

  var selected = form.getAttribute("data-selected-time") || "";
  var requestId = 0;

  function showMessage(text) {
    box.textContent = "";
    var p = document.createElement("p");
    p.className = "muted slots-msg";
    p.textContent = text;
    box.appendChild(p);
  }

  function loadSlots() {
    if (!doctor.value || !date.value) {
      showMessage("Choose a doctor and date to see free times.");
      return;
    }
    var id = ++requestId;
    showMessage("Checking availability...");
    var url = form.getAttribute("data-slots-url") +
      "?doctor=" + encodeURIComponent(doctor.value) +
      "&date=" + encodeURIComponent(date.value);
    var exclude = form.getAttribute("data-exclude");
    if (exclude) url += "&exclude=" + encodeURIComponent(exclude);

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (id !== requestId) return;            // a newer request replaced this one
        if (!data.slots.length) { showMessage(data.message || "No free slots."); return; }
        box.textContent = "";
        data.slots.forEach(function (slot) {
          var label = document.createElement("label");
          label.className = "slot";
          var input = document.createElement("input");
          input.type = "radio";
          input.name = "time";
          input.value = slot.value;
          input.required = true;
          if (slot.value === selected) input.checked = true;
          var span = document.createElement("span");
          span.textContent = slot.label;
          label.appendChild(input);
          label.appendChild(span);
          box.appendChild(label);
        });
      })
      .catch(function () {
        if (id === requestId) showMessage("Could not load times. Check your connection and try again.");
      });
  }

  doctor.addEventListener("change", function () { selected = ""; loadSlots(); });
  date.addEventListener("change", function () { selected = ""; loadSlots(); });
  loadSlots();
});
