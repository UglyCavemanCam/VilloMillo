/* VilloMillo dashboard behaviour. Plain JavaScript, no libraries, no outside requests. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var profile = document.body.dataset.profile || "";
  var drawer = $("#drawer"), backdrop = $("#backdrop"), currentPanel = null, lastFocus = null;

  /* ---- toasts ---- */
  function toast(text, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = text;
    $("#toasts").appendChild(el);
    setTimeout(function () { el.remove(); }, kind === "bad" ? 7000 : 3500);
  }

  /* ---- side panel collapse ---- */
  var collapse = $("#collapse");
  if (collapse) collapse.addEventListener("click", function () {
    var on = document.body.classList.toggle("collapsed");
    try { localStorage.setItem("vm-collapsed", on ? "1" : "0"); } catch (e) { /* private window */ }
  });

  /* ---- drawer ---- */
  var SKELETON = '<div class="drawer-head"><div class="skel" style="width:40%;height:20px"></div><button type="button" class="icon-btn" data-close aria-label="Close">\u00d7</button></div>' +
    '<div class="drawer-body" aria-busy="true"><div class="skel" style="aspect-ratio:9/16;max-height:46vh"></div><div class="skel" style="height:16px"></div><div class="skel" style="height:16px;width:70%"></div><div class="skel" style="height:64px"></div></div>';
  function openPanel(url) {
    lastFocus = lastFocus && drawer.classList.contains("open") ? lastFocus : document.activeElement;
    currentPanel = url;
    if (!drawer.classList.contains("open")) {  /* show the frame at once; a slow disk should not feel like a dead click */
      drawer.innerHTML = SKELETON;
      drawer.classList.add("open"); backdrop.classList.add("open");
      drawer.setAttribute("aria-hidden", "false");
    }
    return fetch(url, { headers: { "X-Requested-With": "panel" } }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 404 ? "That item is gone (it may have been removed)." : "Could not open this (" + r.status + ").");
      return r.text();
    }).then(function (html) {
      if (currentPanel !== url) return;  /* a newer click replaced this one */
      drawer.innerHTML = html;
      var focusTarget = $("[data-close]", drawer); if (focusTarget) focusTarget.focus();
    }).catch(function (e) { closePanel(); toast(e.message, "bad"); });
  }
  function closePanel() {
    drawer.classList.remove("open"); backdrop.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true"); currentPanel = null;
    $$(".clip-card.is-open").forEach(function (c) { c.classList.remove("is-open"); });
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  backdrop.addEventListener("click", closePanel);

  /* ---- confirm dialog ---- */
  var confirmBox = $("#confirm");
  function confirmFirst(title, text) {
    return new Promise(function (resolve) {
      $("#confirm-title").textContent = title || "Are you sure?";
      $("#confirm-text").textContent = text || "";
      var done = function (v) { confirmBox.close(); resolve(v); };
      $("#confirm-yes").onclick = function () { done(true); };
      $("#confirm-no").onclick = function () { done(false); };
      confirmBox.onclose = function () { resolve(false); };
      confirmBox.showModal();
    });
  }

  /* ---- actions: any element with data-act posts a form to the dashboard ---- */
  function send(el, extra) {
    var fields = {};
    try { fields = JSON.parse(el.dataset.fields || "{}"); } catch (e) { /* none */ }
    Object.keys(extra || {}).forEach(function (k) { fields[k] = extra[k]; });
    if (!fields.profile) fields.profile = profile;
    el.classList.add("is-busy");
    return fetch(el.dataset.act, { method: "POST", body: new URLSearchParams(fields) }).then(function (r) {
      return r.text().then(function (body) {
        if (!r.ok) { var msg = body; try { msg = JSON.parse(body).detail || body; } catch (e) { /* plain */ } throw new Error(String(msg).slice(0, 220)); }
        return body;
      });
    }).then(function () {
      if (el.dataset.ok) toast(el.dataset.ok, "good");
      var after = el.dataset.after || "reload";
      if (after === "panel" && currentPanel) return openPanel(currentPanel);
      if (after === "reload") setTimeout(function () { location.reload(); }, el.dataset.ok ? 450 : 0);
    }).catch(function (e) { toast(e.message, "bad"); }).then(function () { el.classList.remove("is-busy"); });
  }
  document.addEventListener("click", function (ev) {
    var t = ev.target;
    var panel = t.closest("[data-panel]");
    if (panel) {
      ev.preventDefault();
      $$(".clip-card.is-open").forEach(function (c) { c.classList.remove("is-open"); });
      if (panel.classList.contains("clip-card")) panel.classList.add("is-open");
      return openPanel(panel.dataset.panel);
    }
    if (t.closest("[data-close]")) return closePanel();
    var cp = t.closest("[data-copy]");
    if (cp) {
      var src = document.getElementById(cp.dataset.copy);
      if (src && navigator.clipboard) navigator.clipboard.writeText(src.textContent.trim()).then(function () {
        var old = cp.textContent; cp.textContent = "Copied"; setTimeout(function () { cp.textContent = old; }, 1500);
      });
      return;
    }
    var act = t.closest("[data-act]");
    if (act && act.tagName !== "FORM") {
      ev.preventDefault();
      if (act.dataset.confirm) {
        confirmFirst(act.dataset.confirmTitle, act.dataset.confirm).then(function (yes) { if (yes) send(act); });
      } else send(act);
    }
  });
  document.addEventListener("submit", function (ev) {
    var f = ev.target.closest("form[data-act]");
    if (!f) return;
    ev.preventDefault();
    var extra = {};
    $$("input[name]", f).forEach(function (i) { extra[i.name] = i.value; });
    send(f, extra);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && drawer.classList.contains("open")) closePanel();
  });

  /* ---- search palette ---- */
  var palette = $("#palette"), input = $("#palette-input"), list = $("#palette-list"), timer = null, items = [], cursor = 0, searching = false;
  function pages() {
    return $$(".sidebar a.nav").map(function (a) { return { type: "Page", title: a.getAttribute("title") || a.textContent.trim(), sub: "", href: a.getAttribute("href") }; });
  }
  function render() {
    list.innerHTML = "";
    var last = "";
    items.forEach(function (it, i) {
      if (it.type !== last) { var g = document.createElement("li"); g.className = "group"; g.textContent = it.type; list.appendChild(g); last = it.type; }
      var li = document.createElement("li"), a = document.createElement("a");
      a.href = it.href; if (it.external) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
      if (i === cursor) a.className = "on";
      a.textContent = it.title;
      if (it.sub) { var s = document.createElement("small"); s.textContent = it.sub; a.appendChild(s); }
      li.appendChild(a); list.appendChild(li);
    });
    if (!items.length) { var e = document.createElement("li"); e.className = "group"; e.textContent = searching ? "Searching\u2026" : "Nothing found"; list.appendChild(e); }
  }
  function search() {
    var q = input.value.trim();
    if (!q) { items = pages(); cursor = 0; return render(); }
    var lower = q.toLowerCase();
    var local = pages().filter(function (p) { return p.title.toLowerCase().indexOf(lower) !== -1; });
    searching = true; items = local; render();
    fetch("/api/search?profile=" + encodeURIComponent(profile) + "&q=" + encodeURIComponent(q)).then(function (r) { return r.json(); })
      .then(function (found) { if (input.value.trim() !== q) return; searching = false; items = local.concat(found.results || []); cursor = 0; render(); })
      .catch(function () { searching = false; items = local; render(); });
  }
  function openPalette() { palette.showModal(); input.value = ""; search(); input.focus(); }
  $("#open-palette").addEventListener("click", openPalette);
  $("#open-palette").addEventListener("focus", function (e) { e.target.blur(); openPalette(); });
  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); palette.open ? palette.close() : openPalette(); }
  });
  input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(search, 160); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault(); cursor = Math.max(0, Math.min(items.length - 1, cursor + (ev.key === "ArrowDown" ? 1 : -1))); render();
    } else if (ev.key === "Enter" && items[cursor]) { location.href = items[cursor].href; }
  });
  palette.addEventListener("click", function (ev) { if (ev.target === palette) palette.close(); });

  /* ---- keyboard shortcuts: g then a letter goes to a page; / searches; [ folds the side panel; ? lists them ---- */
  var shortcuts = $("#shortcuts"), goKeys = { h: "/?", c: "/clips", p: "/calendar", a: "/accounts", i: "/insights", s: "/settings" }, going = 0;
  function typing(t) { return t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable); }
  document.addEventListener("keydown", function (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || typing(ev.target) || $("dialog[open]")) return;
    var k = ev.key;
    if (going && Date.now() - going < 1200 && goKeys[k.toLowerCase()]) {
      going = 0; var link = $('.sidebar a.nav[href^="' + goKeys[k.toLowerCase()] + '"]'); if (link) location.href = link.href; return;
    }
    going = 0;
    if (k === "g") going = Date.now();
    else if (k === "/") { ev.preventDefault(); openPalette(); }
    else if (k === "[" && collapse) collapse.click();
    else if (k === "?" && shortcuts) shortcuts.showModal();
  });
  var shortcutsBtn = $("#open-shortcuts"); if (shortcutsBtn && shortcuts) shortcutsBtn.addEventListener("click", function () { shortcuts.showModal(); });
  if (shortcuts) shortcuts.addEventListener("click", function (ev) { if (ev.target === shortcuts || ev.target.closest("[data-close-dialog]")) shortcuts.close(); });

  /* ---- live clip progress: only while the engine reports a job ---- */
  var live = $("[data-live]");
  if (live) {
    var poll = function () {
      fetch("/api/studio?profile=" + encodeURIComponent(profile)).then(function (r) { return r.json(); }).then(function (d) {
        var bar = $("[data-live-bar]"); if (bar) bar.style.width = d.percent + "%";
        var title = $("[data-live-title]");
        if (title) title.textContent = d.error ? "Stopped: " + d.error.slice(0, 160) : d.job && d.job.status === "done" ? "Finished" : "Working";
        var steps = $("[data-live-steps]");
        if (steps) $$(".step", steps).forEach(function (s, i) { var st = d.steps[i] ? d.steps[i].state : ""; s.className = "step " + (st === "done" ? "done" : st === "now" ? "now" : ""); });
        if (d.job && ["submitted", "running", "queued"].indexOf(d.job.status) !== -1) setTimeout(poll, 3000);
        else if (d.job && live.dataset.reload !== "no") setTimeout(function () { location.reload(); }, 1500);
      }).catch(function () { setTimeout(poll, 15000); });
    };
    if (live.dataset.active === "1") setTimeout(poll, 3000);
  }

  /* ---- deep link: ?panel=/clips/5/panel opens that drawer on load ---- */
  var deep = new URLSearchParams(location.search).get("panel");
  if (deep && deep.charAt(0) === "/" && deep.charAt(1) !== "/") openPanel(deep + (deep.indexOf("?") === -1 ? "?" : "&") + "profile=" + encodeURIComponent(profile));
})();
