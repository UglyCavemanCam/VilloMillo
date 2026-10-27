/* The Formats page: tabs, presets, assets, music, AI Generate and the format builder. Reads its data from <script id="boot">. */
(function () {
  var boot = JSON.parse(document.getElementById("boot").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var current = null, copy = function (x) { return JSON.parse(JSON.stringify(x)); };

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === "text") n.textContent = attrs[k]; else n.setAttribute(k, attrs[k]); });
    (kids || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }
  function say(text, kind) { var n = $("note"); n.textContent = ""; if (text) { n.appendChild(el("div", { "class": "fmt-msg " + (kind || ""), text: text })); window.scrollTo(0, 0); } }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); });
  }
  function options(select, items, blank) {
    select.textContent = "";
    if (blank) select.appendChild(el("option", { value: "", text: blank }));
    items.forEach(function (i) { select.appendChild(el("option", { value: i.id, text: i.label })); });
  }

  function tab(name) {
    ["presets", "templates", "assets", "music"].forEach(function (t) {
      $("tab-" + t).hidden = t !== name; $("tab-btn-" + t).setAttribute("aria-selected", t === name ? "true" : "false");
    });
    try { history.replaceState(null, "", "#" + name); } catch (e) {}
  }
  document.querySelectorAll("[data-tab]").forEach(function (b) { b.addEventListener("click", function () { tab(b.dataset.tab); }); });

  // ---- quick presets and the caption style drawings --------------------------------------------------------------
  function renderPresets() {
    var box = $("preset-list"); box.textContent = "";
    if (!boot.presets.length) box.appendChild(el("p", { "class": "muted", text: "No quick presets left." }));
    boot.presets.forEach(function (p) {
      var sel = el("select", { "class": "field" }); boot.categories.forEach(function (c) { sel.appendChild(el("option", { value: c, text: c })); });
      var use = el("button", { "class": "btn btn-primary btn-sm", type: "button", text: "Use for this kind" });
      var cp = el("button", { "class": "btn btn-sm", type: "button", text: "Copy to My Templates" });
      var del = el("button", { "class": "btn btn-sm btn-ghost", type: "button", text: "Delete forever" });
      use.addEventListener("click", function () { post("/api/formats/preset", { preset_id: p.id, action: "use", category: sel.value }).then(done("Saved, and used for " + sel.value + "."))});
      cp.addEventListener("click", function () { post("/api/formats/preset", { preset_id: p.id, action: "copy" }).then(done("Copied to My Templates."))});
      del.addEventListener("click", function () {
        if (confirm("Delete the " + p.name + " preset for good? It will not come back.")) post("/api/formats/preset-delete", { preset_id: p.id }).then(done("Deleted."));
      });
      box.appendChild(el("div", { "class": "card fmt-card" }, [el("h3", { text: p.name }), el("p", { "class": "muted", style: "margin:0", text: p.blurb }),
        el("label", { style: "font-size:13px" }, [document.createTextNode("Kind of clip "), sel]), el("div", { "class": "cluster" }, [use, cp, del])]));
    });
  }
  function done(message) {
    return function (r) {
      if (!r.ok) return say(r.data.error || "That did not work.", "bad");
      var n = r.data.queued || 0;  // clips already waiting for this kind get the format too
      if (n) message += " " + n + " waiting clip" + (n === 1 ? " is" : "s are") + " being given it now: watch them on the Clips page.";
      try { sessionStorage.setItem("fmt-note", message); } catch (e) {}
      location.hash = "#templates"; location.reload();
    };
  }

  var DRAW = {
    spotlight: "font:800 22px Montserrat;text-transform:uppercase;-webkit-text-stroke:5px #0c0e12;paint-order:stroke fill",
    pop: "font:900 24px Montserrat;text-transform:uppercase;-webkit-text-stroke:5px #0c0e12;paint-order:stroke fill",
    block: "font:800 21px Montserrat;text-transform:uppercase",
    dim: "font:600 20px Poppins", slam: "font:400 46px Anton;text-transform:uppercase;-webkit-text-stroke:5px #0c0e12;paint-order:stroke fill",
    plate: "font:600 17px Inter;background:rgba(0,0,0,.72);padding:8px 14px;border-radius:4px", neon: "font:italic 800 30px 'Barlow Condensed';text-transform:uppercase;color:#5CF0FF;text-shadow:0 0 5px #5CF0FF,0 0 14px #FF2BD6",
    type: "font:400 19px 'Special Elite'", marker: "font:400 22px 'Permanent Marker';transform:rotate(-2.5deg)",
    build: "font:700 21px Poppins", underline: "font:700 21px Montserrat", burst: "font:400 30px Bangers;color:#FFE135;text-transform:uppercase;-webkit-text-stroke:5px #111;paint-order:stroke fill;transform:rotate(-3deg)"
  };
  function renderStyles() {
    var box = $("style-list"); box.textContent = "";
    boot.styles.forEach(function (s) {
      var words = ["shut", "it", "down."].map(function (w, i) {
        var n = el("span", { text: (s.upper ? w.toUpperCase() : w) + " ", style: i === 2 ? "color:" + s.accent : "" }); return n; });
      var line = el("b", { style: (DRAW[s.id] || "") + ";color:" + (["neon", "burst"].indexOf(s.id) > -1 ? "" : "#fff") }, words);
      var use = el("button", { "class": "btn btn-sm", type: "button", text: "Use in the builder" });
      use.addEventListener("click", function () { openBuilder(null, s.id); });
      box.appendChild(el("div", { "class": "fmt-style" }, [el("div", { "class": "fmt-stage" }, [line]),
        el("div", { "class": "info" }, [el("strong", { text: s.label }), el("span", { "class": "muted", text: s.blurb }), use])]));
    });
  }

  // ---- templates ---------------------------------------------------------------------------------------------------
  function renderTemplates() {
    var box = $("template-list"); box.textContent = "";
    if (!boot.templates.length) box.appendChild(el("p", { "class": "muted", text: "No templates yet. Use a quick preset, or press New template." }));
    boot.templates.forEach(function (t) {
      var edit = el("button", { "class": "btn btn-sm btn-primary", type: "button", text: "Edit" });
      edit.addEventListener("click", function () { openBuilder(t); });
      var used = t.categories.length ? "Used for: " + t.categories.join(", ") : "Not used for any kind of clip yet";
      var aiTag = t.design.made_by === "ai" ? el("span", { "class": "muted", style: "font-size:12px;border:1px solid currentColor;border-radius:6px;padding:0 6px", text: "AI made" }) : el("span");
      box.appendChild(el("div", { "class": "card fmt-card" }, [el("h3", { text: t.name }), aiTag, el("p", { "class": "muted", style: "margin:0", text: used }),
        el("div", { "class": "muted", style: "font-size:13px", text: boot.styles.filter(function (s) { return s.id === t.design.captions.style; })[0].label + " captions · " + (t.design.layout === "face_fill" ? "Face fill" : "Fit with blur") }),
        el("div", { "class": "cluster" }, [edit])]));
    });
  }


  function renderFonts() {
    var box = $("font-list"); box.textContent = "";
    if (!boot.font_uploads.length) box.appendChild(el("p", { "class": "muted", text: "No uploaded fonts yet. The bundled fonts are always there." }));
    boot.font_uploads.forEach(function (f) {
      var del = el("button", { "class": "btn btn-sm", type: "button", text: "Delete" });
      del.addEventListener("click", function () { if (confirm("Delete this font for good?")) post("/api/fonts/delete", { id: f.id }).then(doneFonts("Font deleted.")); });
      box.appendChild(el("div", { "class": "cluster", style: "justify-content:space-between" }, [
        el("div", {}, [el("b", { text: f.label }), el("div", { "class": "muted", style: "font-size:12px", text: f.source + ". " + f.licence })]), del]));
    });
  }
  function doneFonts(message) {
    return function (r) {
      if (!r.ok) return say(r.data.error || "That did not work.", "bad");
      try { sessionStorage.setItem("fmt-note", message); } catch (e) {}
      location.hash = "#assets"; location.reload();
    };
  }
  $("font-upload").addEventListener("click", function () {
    var f = $("font-file").files[0];
    if (!f) return say("Choose a TTF or OTF font first.", "bad");
    var q = "filename=" + encodeURIComponent(f.name) + "&name=" + encodeURIComponent($("font-name").value)
      + "&source=" + encodeURIComponent($("font-source").value) + "&licence=" + encodeURIComponent($("font-licence").value);
    fetch("/api/fonts/upload?" + q, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); }).then(doneFonts("Font uploaded."));
  });

  function renderMusic() {
    var m = boot.music, have = m.checklist.filter(function (c) { return c.have; }).length;
    $("music-count").textContent = m.tracks.length + " track" + (m.tracks.length === 1 ? "" : "s") + " in the library. " + have + " of " + m.checklist.length + " on the shortlist.";
    $("music-where").textContent = "Looking in " + m.downloads;
    var box = $("music-tracks"); box.textContent = "";
    m.tracks.forEach(function (t) {
      var mood = el("select", { "class": "field" });
      m.moods.forEach(function (x) { var o = el("option", { value: x, text: x }); if (x === t.mood) o.selected = true; mood.appendChild(o); });
      var cats = el("div", { "class": "chip-row" });
      m.categories.forEach(function (c) {
        var cb = el("input", { type: "checkbox", value: c }); cb.checked = t.categories.indexOf(c) > -1;
        cats.appendChild(el("label", { "class": "inline", style: "display:inline-flex;gap:6px;align-items:center" }, [cb, document.createTextNode(c)]));
      });
      var save = el("button", { "class": "btn btn-sm", type: "button", text: "Save" });
      var del = el("button", { "class": "btn btn-sm", type: "button", text: "Delete" });
      save.addEventListener("click", function () {
        post("/api/music/update", { id: t.id, mood: mood.value, categories: Array.prototype.map.call(cats.querySelectorAll("input:checked"), function (i) { return i.value; }) }).then(doneMusic("Saved."));
      });
      del.addEventListener("click", function () { if (confirm("Delete this track for good?")) post("/api/music/delete", { id: t.id }).then(doneMusic("Track deleted.")); });
      var length = Math.floor(t.duration_s / 60) + ":" + ("0" + Math.round(t.duration_s % 60)).slice(-2);
      box.appendChild(el("div", { "class": "card stack" }, [
        el("div", { "class": "cluster", style: "justify-content:space-between" }, [
          el("div", {}, [el("b", { text: t.title }), el("div", { "class": "muted", style: "font-size:13px", text: t.artist + " · " + length + (t.genre ? " · " + t.genre : "") })]),
          el("audio", { controls: "", preload: "none", src: "/music/" + t.id + "/file", style: "height:34px;max-width:100%" })]),
        el("div", { "class": "fmt-row" }, [mood, cats, save, del]),
        t.singing !== null && t.singing >= 0.35 ? el("div", { style: "font-size:13px;color:#e0a000", text: "Warning: this track sounds sung. Under a voice it may compete with the speaker; the app will still use it when it is the only fit." }) : el("span"),
        el("div", { "class": "muted", style: "font-size:12px", text: t.source + ". " + t.licence + " Added " + t.added_at.slice(0, 10) + "." })]));
    });
    var miss = $("music-missing"); miss.textContent = "";
    var left = m.checklist.filter(function (c) { return !c.have; });
    if (!left.length) miss.appendChild(el("p", { "class": "muted", text: "Every shortlisted track is in the library." }));
    left.forEach(function (c) {
      miss.appendChild(el("div", { "class": "cluster", style: "justify-content:space-between" }, [
        el("span", { text: c.title + " \u2014 " + c.artist }), el("span", { "class": "muted", style: "font-size:13px", text: c.mood + " \u00b7 " + c.categories.join(", ") })]));
    });
    options($("mn_mood"), m.moods.map(function (x) { return { id: x, label: x }; }));
    var mc = $("mn_cats"); mc.textContent = "";
    m.categories.forEach(function (c) { mc.appendChild(el("label", { "class": "inline", style: "display:inline-flex;gap:6px;align-items:center" }, [el("input", { type: "checkbox", value: c }), document.createTextNode(c)])); });
    $("mn_source").value = m.source; $("mn_licence").value = m.licence;
  }
  function doneMusic(message) {
    return function (r) {
      if (!r.ok) return say(r.data.error || "That did not work.", "bad");
      try { sessionStorage.setItem("fmt-note", message); } catch (e) {}
      location.hash = "#music"; location.reload();
    };
  }
  $("music-import").addEventListener("click", function () {
    $("music-import").disabled = true;
    post("/api/music/import", {}).then(function (r) {
      $("music-import").disabled = false;
      if (!r.ok) return say(r.data.error || "That did not work.", "bad");
      var d = r.data, parts = [];
      if (d.imported.length) parts.push("Added " + d.imported.length + ": " + d.imported.join(", ") + ".");
      if (d.already.length) parts.push(d.already.length + " already in the library.");
      if (d.problems.length) parts.push("Could not use: " + d.problems.join(" "));
      if (!parts.length) parts.push("No new shortlisted tracks were found in your Downloads folder.");
      try { sessionStorage.setItem("fmt-note", parts.join(" ")); } catch (e) {}
      location.hash = "#music"; location.reload();
    });
  });
  $("mn_add").addEventListener("click", function () {
    var f = $("mn_file").files[0];
    if (!f) return say("Choose an MP3 or WAV file first.", "bad");
    var cats = Array.prototype.map.call($("mn_cats").querySelectorAll("input:checked"), function (i) { return i.value; }).join(",");
    var q = "filename=" + encodeURIComponent(f.name) + "&title=" + encodeURIComponent($("mn_title").value) + "&artist=" + encodeURIComponent($("mn_artist").value)
      + "&mood=" + encodeURIComponent($("mn_mood").value) + "&genre=" + encodeURIComponent($("mn_genre").value) + "&categories=" + encodeURIComponent(cats)
      + "&source=" + encodeURIComponent($("mn_source").value) + "&licence=" + encodeURIComponent($("mn_licence").value);
    fetch("/api/music/upload?" + q, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); }).then(doneMusic("Track added."));
  });

  function renderLogos() {
    var box = $("logo-list"); box.textContent = "";
    if (!boot.logos.length) box.appendChild(el("p", { "class": "muted", text: "No logo yet." }));
    boot.logos.forEach(function (l) {
      var img = el("img", { src: "/assets/" + l.id + "/file", alt: l.name, style: "height:56px;max-width:180px;object-fit:contain;border-radius:8px" });
      var del = el("button", { "class": "btn btn-sm", type: "button", text: "Delete" });
      del.addEventListener("click", function () { if (confirm("Delete this logo for good?")) post("/api/assets/delete", { id: l.id }).then(done("Logo deleted.")); });
      box.appendChild(el("div", { "class": "cluster", style: "justify-content:space-between" }, [img, el("span", { text: l.name }), del]));
    });
  }
  $("logo-upload").addEventListener("click", function () {
    var f = $("logo-file").files[0];
    if (!f) return say("Choose a PNG, WebP or GIF first.", "bad");
    fetch("/api/assets/logo?name=" + encodeURIComponent(f.name), { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
      .then(function (r) {
        if (!r.ok) return say(r.data.error || "That did not work.", "bad");
        try { sessionStorage.setItem("fmt-note", "Logo uploaded."); } catch (e) {}
        location.hash = "#assets"; location.reload();
      });
  });


  function save(asNew) {
    var body = { id: asNew ? null : current.id, name: $("f_name").value, design: read(), categories: chosenCategories() };
    post("/api/formats/save", body).then(done("Saved."));
  }
  $("save").addEventListener("click", function () { save(false); });
  $("save-copy").addEventListener("click", function () { save(true); });
  $("close").addEventListener("click", function () { $("builder").hidden = true; pause(); });
  $("new-template").addEventListener("click", function () { openBuilder(null); });
  // ---- AI Generate: three formats from a few answers, each with a preview; nothing saved until one is picked --------
  function aiAnswers() {
    var logo = $("ai_logo").value;
    return { kind: $("ai_kind").value, feel: $("ai_feel").value, campaign: $("ai_campaign").checked,
             logo_id: $("ai_campaign").checked || !logo ? null : Number(logo), note: $("ai_note").value.trim() };
  }
  function openAi() {
    options($("ai_logo"), boot.logos.map(function (l) { return { id: l.id, label: l.name }; }), "No logo");
    $("ai-results").textContent = ""; $("ai-status").textContent = ""; $("ai-modal").hidden = false;
  }
  function renderAiOptions(data) {
    var box = $("ai-results"); box.textContent = "";
    if (data.dropped && data.dropped.length) box.appendChild(el("p", { "class": "muted", style: "font-size:13px", text: "Left out because they did not pass the checks: " + data.dropped.join("; ") }));
    data.options.forEach(function (opt, i) {
      var video = el("video", { src: opt.preview, controls: "", muted: "", loop: "", playsinline: "", style: "width:180px;max-width:100%;border-radius:10px;background:#000" });
      var pick = el("button", { "class": "btn btn-primary btn-sm", type: "button", text: "Use this one" });
      pick.addEventListener("click", function () { askName(opt, answersUsed); });
      var more = el("button", { "class": "btn btn-sm", type: "button", text: "More like this" });
      var less = el("button", { "class": "btn btn-sm", type: "button", text: "Not this" });
      var note = el("span", { class: "muted", style: "font-size:13px" });
      function feedback(verdict, button) {
        post("/api/formats/generate/feedback", Object.assign({}, answersUsed, { design: opt.design, verdict: verdict }))
          .then(function (r) {
            if (!r.ok) return say(r.data.error || "That did not work.", "bad");
            more.disabled = less.disabled = true; note.textContent = verdict === "more" ? "Noted: more like this." : "Noted: not this.";
          });
      }
      more.addEventListener("click", function () { feedback("more", more); });
      less.addEventListener("click", function () { feedback("less", less); });
      box.appendChild(el("div", { "class": "card cluster", style: "align-items:flex-start;gap:14px" }, [video,
        el("div", { style: "min-width:0;flex:1 1 220px" }, [el("b", { text: "Option " + (i + 1) + ": " + opt.name }),
          el("p", { "class": "muted", style: "margin:4px 0 8px", text: opt.summary }),
          el("div", { "class": "cluster" }, [pick, more, less, note])])]));
    });
    box.appendChild(el("div", { id: "ai-pick" }));
  }
  var answersUsed = null;
  function askName(opt, answers) {
    var host = $("ai-pick"); host.textContent = "";
    var name = el("input", { "class": "field", type: "text", value: opt.name, maxlength: "40" });
    var cats = el("div", { "class": "chip-row" });
    var want = { podcast: "Podcasts", talk: "Talks", stream: "Stream clips", other: "Other" }[answers.kind];
    boot.categories.forEach(function (c) {
      var cb = el("input", { type: "checkbox", value: c }); cb.checked = c === want;
      cats.appendChild(el("label", { "class": "inline", style: "display:inline-flex;gap:6px;align-items:center" }, [cb, document.createTextNode(c)]));
    });
    var save = el("button", { "class": "btn btn-primary", type: "button", text: "Save this format" });
    save.addEventListener("click", function () {
      var chosen = Array.prototype.map.call(cats.querySelectorAll("input:checked"), function (i) { return i.value; });
      post("/api/formats/generate/pick", { name: name.value, design: opt.design, categories: chosen, answers: answers,
        kind: answers.kind, feel: answers.feel, campaign: answers.campaign, logo_id: answers.logo_id, note: answers.note })
        .then(function (r) {
          if (!r.ok) return say(r.data.error || "That did not work.", "bad");
          var n = r.data.queued || 0;
          try { sessionStorage.setItem("fmt-note", "Saved \"" + name.value + "\"." + (n ? " " + n + " waiting clip" + (n === 1 ? " is" : "s are") + " being given it now: watch them on the Clips page." : "")); } catch (e) {}
          location.hash = "#templates"; location.reload();
        });
    });
    host.appendChild(el("div", { "class": "card stack" }, [el("b", { text: "Name it and choose the kinds of clips that use it" }), name, cats, el("div", {}, [save])]));
    host.scrollIntoView({ behavior: "smooth" });
  }
  $("ai-generate").addEventListener("click", openAi);
  $("ai-close").addEventListener("click", function () { $("ai-modal").hidden = true; });
  $("ai-go").addEventListener("click", function () {
    var answers = aiAnswers(); answersUsed = answers;
    var go = $("ai-go"), status = $("ai-status");
    go.disabled = true; status.textContent = "Making three formats and their previews. This takes about a minute.";
    $("ai-results").textContent = "";
    post("/api/formats/generate", Object.assign({ profile: boot.profile }, answers)).then(function (r) {
      go.disabled = false;
      if (!r.ok) { status.textContent = ""; return say(r.data.error || "That did not work.", "bad"); }
      status.textContent = r.data.options.length + " formats made. Cost: $" + r.data.cost.toFixed(2) + ".";
      renderAiOptions(r.data);
    });
  });

  $("delete").addEventListener("click", function () {
    var t = boot.templates.filter(function (x) { return x.id === current.id; })[0];
    var warn = t && t.categories.length ? " " + t.categories.join(", ") + " will have no format, so its clips wait for review." : "";
    if (confirm("Delete this template for good?" + warn)) post("/api/formats/delete", { id: current.id }).then(done("Deleted."));
  });
  $("webcam-suggest").addEventListener("click", function () {
    var status = $("webcam-suggest"); status.disabled = true;
    post("/api/formats/suggest-webcam", { video_id: $("v_video").value }).then(function (r) {
      status.disabled = false;
      if (!r.ok) return say(r.data.error || "No webcam was found in that video.", "bad");
      $("w_x").value = r.data.x; $("w_y").value = r.data.y; $("w_w").value = r.data.w; $("w_h").value = r.data.h;
      loadStage(); say("Webcam box suggested. Check it against the preview.", "good");
    });
  });
  // Editing a real clip has three ways to keep the look. Apply re-makes this clip only; the other two keep it as a format and leave the clip alone.
  $("apply-clip").addEventListener("click", function () {
    var src = boot.clip.source_template;
    $("apply-clip").disabled = true;
    post("/api/formats/apply-clip", { profile: boot.clip.profile, clip_id: boot.clip.id, design: read(), template_id: src ? src.id : null }).then(function (r) {
      $("apply-clip").disabled = false;
      var after = boot.clip.waiting_post ? " Its post keeps its place in the queue and will send the new file." : "";
      say(r.ok ? "Making this clip again with this look. It takes about a minute: open the clip on the Clips page to watch it." + after : (r.data.error || "That did not work."), r.ok ? "good" : "bad");
    });
  });
  $("clip-new").addEventListener("click", function () {
    var src = boot.clip.source_template, name = $("f_name").value.trim();
    if (src && name === src.name) name += " (edited)";
    post("/api/formats/save", { id: null, name: name, design: read(), categories: [] })   // a new format takes no kind of clip from the old one
      .then(done("Saved as a new format called \"" + name + "\". It is not used for any kind of clip yet. This clip was not changed."));
  });
  $("clip-update").addEventListener("click", function () {
    var src = boot.clip.source_template;
    if (!confirm("Change the \"" + src.name + "\" format? Clips made from now on use the new look. Clips already made keep theirs, including this one.")) return;
    post("/api/formats/save", { id: src.id, name: $("f_name").value, design: read(), categories: chosenCategories() })
      .then(done("Updated the \"" + src.name + "\" format. Clips made from now on use it; clips already made were not changed."));
  });
  // ---- the builder -----------------------------------------------------------------------------------------------------
  // A design is the format-wide settings (layout, music, effects, ...) plus `elements`, the list of everything drawn on the picture,
  // back to front. The left side adds, orders and hides elements; the right side edits the selected one; the picture shows them all.
  var design = null, selected = null, current = null;
  var geo = boot.geometry, stageData = null, stageReq = 0, dragging = null, guides = [], checkedAt = 0, checkReq = 0, checkTimer = null;
  var LEGACY_KEYS = ["captions", "headline", "chips", "takeaway", "logo", "cta", "brand", "place"];   // the old switches; the elements replace them
  var BRAND = ["handle", "logo", "button", "progress_bar"];                                          // never on a campaign clip
  var SPOKEN_ONLY = ["label", "captions"], SPOKEN_OK = ["headline", "takeaway", "label", "captions"];
  var BLURB = {
    headline: "The AI's short title at the start", captions: "The spoken words, as they are said", label: "A tag for each point the speaker makes",
    takeaway: "A short closing line", handle: "Your channel handle", text: "Any words you type", logo: "Your logo", shape: "A box or a bar of colour",
    button: "A call to action near the end", progress_bar: "A thin bar that fills as the clip plays"
  };
  var TYPE_LABEL = {};
  boot.element_types.forEach(function (t) { TYPE_LABEL[t.id] = t.label; });

  var A = window.FormatsActions;   // the builder's edits as plain functions (formats-actions.js)
  A.configure(boot.geometry.defaults);   // the recipe's own numbers, so the stage draws what the render draws
  function byId(id) { return design ? design.elements.filter(function (e) { return e.id === id; })[0] || null : null; }
  function getp(o, path) { return path.split(".").reduce(function (a, k) { return a == null ? a : a[k]; }, o); }
  function setp(o, path, v) { var ks = path.split("."), last = ks.pop(); ks.reduce(function (a, k) { return a[k]; }, o)[last] = v; }
  function clamp(v, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, v))); }
  function px(n) { return (n / geo.width * 100) + "cqw"; }   // a size in the 1080-wide picture, as a share of the stage's width
  function pct(y) { return (y / geo.height * 100) + "%"; }
  function barH(e) { return A.barHeight(e); }

  // ---- what each kind of element lets you change ----------------------------------------------------------------------
  function N(path, label, min, max, blank) { return { k: "num", path: path, label: label, min: min, max: max, blank: blank }; }
  function C(path, label, blank) { return { k: "col", path: path, label: label, blank: blank }; }
  function S(path, label, opts, blank, after) { return { k: "sel", path: path, label: label, opts: opts, blank: blank, after: after }; }
  function K(path, label) { return { k: "chk", path: path, label: label }; }
  function T(path, label, max, rows) { return { k: "txt", path: path, label: label, max: max, rows: rows }; }
  function U(path, label) { return { k: "caps", path: path, label: label }; }
  var FONTS = function () { return boot.fonts; };
  var NO_BOX = { label: "No box", value: "" };
  var X = N("x", "Across, from the left (x)", 0, 1080, "Layout's own spot"), Y = N("y", "Down, from the top (y)", 0, 1920, "Layout's own spot");
  var TEXT_BOX_LOOK = [S("style.font", "Font", FONTS), N("style.size", "Size", 16, 160), C("style.color", "Text colour"), C("style.box", "Box colour", NO_BOX),
    N("style.radius", "Corner radius", 0, 100, "Round as a pill"), N("style.pad_x", "Side padding", 0, 120), N("style.pad_y", "Top and bottom padding", 0, 120),
    C("style.outline_color", "Outline colour"), N("style.outline_w", "Outline width", 0, 12), K("style.shadow", "Shadow"), K("style.uppercase", "All capitals"),
    N("style.opacity", "Opacity (%)", 10, 100)];
  var SCHEMA = {
    headline: {
      text: [T("text.default", "Example headline (the real one is written for each clip)", 200, 3)],
      look: [S("style.font", "Font", FONTS), N("style.size", "Size", 24, 90), S("style.align", "Alignment", [{ id: "left", label: "Left" }, { id: "center", label: "Centre" }, { id: "right", label: "Right" }]),
        C("style.color", "Text colour"), C("style.box", "Box colour", NO_BOX), N("style.radius", "Corner radius", 0, 40), N("style.width_pct", "Width (% of the picture)", 40, 100),
        C("style.outline_color", "Outline colour"), N("style.outline_w", "Outline width", 0, 12, "Automatic"), K("style.shadow", "Shadow"), N("style.opacity", "Opacity (%)", 10, 100)],
      pos: [Y]
    },
    captions: {
      look: [S("options.style", "Look", function () { return boot.styles; }), S("options.position", "Position", [{ id: "lower", label: "Lower" }, { id: "middle", label: "Middle" }, { id: "upper", label: "Upper" }], null,
          function (e) { e.y = null; }),
        S("style.font", "Font", FONTS, "The look's own"), N("style.size", "Size", 40, 160, "The look's own"), U("style.uppercase", "Capitals"),
        C("style.text_color", "Word colour", { label: "The look's own", value: null }), C("style.highlight_color", "Spoken word colour", { label: "The look's own", value: null })],
      pos: [Y]
    },
    label: {
      text: [T("text.default", "Example text (the real one is the point being made)", 200, 1), K("options.numbered", "Number each label (1 · …)")],
      look: TEXT_BOX_LOOK, pos: [X, Y]
    },
    takeaway: {
      text: [T("text.default", "Example takeaway (the real one is written for each clip)", 200, 1)],
      look: TEXT_BOX_LOOK, pos: [X, Y]
    },
    handle: {
      text: [T("text.default", "Example handle (the real one is your channel's)", 200, 1)],
      look: [S("style.font", "Font", FONTS), N("style.size", "Size", 12, 120, "Automatic"), C("style.color", "Text colour"), N("style.opacity", "Opacity (%)", 5, 100, "Automatic"),
        C("style.box", "Box colour", NO_BOX), N("style.radius", "Corner radius", 0, 100, "Automatic"), N("style.pad_x", "Side padding", 0, 120), N("style.pad_y", "Top and bottom padding", 0, 120),
        C("style.outline_color", "Outline colour"), N("style.outline_w", "Outline width", 0, 12)],
      pos: [X, Y]
    },
    text: { text: [T("text.default", "The words", 200, 2)], look: TEXT_BOX_LOOK, pos: [X, Y] },
    logo: {
      look: [S("options.asset_id", "Logo", function () { return boot.logos.map(function (l) { return { id: l.id, label: l.name }; }); }, "Choose a logo"),
        N("style.size", "Size (% of the picture's width)", 6, 30), N("style.opacity", "Opacity (%)", 10, 100),
        S("options.corner", "Corner", [{ id: "left", label: "Left (top)" }, { id: "right", label: "Right (top)" }], null, function (e) { e.x = null; }),
        N("options.margin", "Distance from the edge", 0, 300, "Automatic")],
      pos: [N("x", "Left edge (x)", 0, 1080, "Layout's own spot"), N("y", "Top edge (y)", 0, 1920, "Layout's own spot")]
    },
    shape: {
      look: [C("style.color", "Colour"), N("style.radius", "Corner radius", 0, 960), N("style.opacity", "Opacity (%)", 5, 100), C("style.outline_color", "Outline colour"), N("style.outline_w", "Outline width", 0, 40)],
      pos: [X, Y, N("w", "Width", 4, 1080), N("h", "Height", 4, 1920)]
    },
    button: { text: [T("text.default", "The words", 40, 1)], look: TEXT_BOX_LOOK, pos: [X, Y] },
    progress_bar: {
      look: [C("style.color", "Bar colour"), C("style.track_color", "Track colour"), N("style.track_opacity", "Track opacity (%)", 0, 100), N("style.height", "Thickness", 2, 80, "Automatic")],
      pos: [N("y", "Top edge (y)", 0, 1920, "The bottom of the picture")]
    }
  };

  function optionList(spec) { return typeof spec.opts === "function" ? spec.opts() : spec.opts; }
  // One control for one setting of one element. It writes straight into the element, and the picture follows at once.
  function control(e, s) {
    var v = getp(e, s.path), box;
    function put(value) { setp(e, s.path, value); if (s.after) s.after(e); onEdit(); }
    if (s.k === "chk") {
      var cb = el("input", { type: "checkbox" }); cb.checked = !!v;
      cb.addEventListener("change", function () { put(cb.checked); });
      return el("label", { "class": "fld inline" }, [cb, document.createTextNode(s.label)]);
    }
    if (s.k === "num") {
      var num = el("input", { "class": "field", type: "number", min: s.min, max: s.max }); if (s.blank) num.placeholder = s.blank;
      num.value = v == null ? "" : v;
      num.addEventListener("input", function () { put(num.value === "" ? null : Number(num.value)); });
      return el("label", { "class": "fld" }, [document.createTextNode(s.label), num]);
    }
    if (s.k === "txt") {
      var t = s.rows > 1 ? el("textarea", { "class": "field", rows: s.rows }) : el("input", { "class": "field", type: "text" });
      t.maxLength = s.max; t.value = v == null ? "" : v;
      t.addEventListener("input", function () { put(t.value); });
      return el("label", { "class": "fld wide" }, [document.createTextNode(s.label), t]);
    }
    if (s.k === "sel" || s.k === "caps") {
      var sel = el("select", { "class": "field" });
      if (s.k === "caps") {
        [["", "The look's own"], ["1", "All capitals"], ["0", "Normal"]].forEach(function (o) { sel.appendChild(el("option", { value: o[0], text: o[1] })); });
        sel.value = v === null || v === undefined ? "" : (v ? "1" : "0");
        sel.addEventListener("change", function () { put(sel.value === "" ? null : sel.value === "1"); });
      } else {
        if (s.blank) sel.appendChild(el("option", { value: "", text: s.blank }));
        optionList(s).forEach(function (o) { sel.appendChild(el("option", { value: o.id, text: o.label })); });
        sel.value = v == null ? "" : String(v);
        sel.addEventListener("change", function () { put(sel.value === "" ? null : (typeof v === "number" || s.path === "options.asset_id" ? Number(sel.value) : sel.value)); });
      }
      return el("label", { "class": "fld" }, [document.createTextNode(s.label), sel]);
    }
    // a colour, with a tick for "none" when the setting may be left without one
    var blankOn = !!s.blank && (v === undefined ? null : v) === s.blank.value;
    var pick = el("input", { type: "color" }); pick.value = (typeof v === "string" && v ? v : "#ffb000").toLowerCase(); pick.disabled = blankOn;
    pick.addEventListener("input", function () { put(pick.value.toUpperCase()); });
    var kids = [pick];
    if (s.blank) {
      var none = el("input", { type: "checkbox" }); none.checked = blankOn;
      none.addEventListener("change", function () { pick.disabled = none.checked; put(none.checked ? s.blank.value : pick.value.toUpperCase()); });
      kids.push(el("span", { "class": "blank" }, [none, document.createTextNode(s.blank.label)]));
    }
    box = el("div", { "class": "fld" }, [document.createTextNode(s.label), el("div", { "class": "colour-row" }, kids)]);
    return box;
  }

  function section(title, controls, note) {
    var fs = el("fieldset", {}, [el("legend", { text: title })]);
    if (note) fs.appendChild(el("p", { "class": "muted", style: "margin:0;font-size:13px", text: note }));
    controls.forEach(function (c) { fs.appendChild(c); });
    return fs;
  }
  function timingControls(e) {
    var t = e.timing, kind = e.type, out = [];
    var modes = [{ id: "whole", label: "The whole clip" }, { id: "first", label: "The first seconds" }, { id: "last", label: "The last seconds" }, { id: "range", label: "A set time range" }];
    if (SPOKEN_OK.indexOf(kind) > -1) modes.unshift({ id: "spoken", label: "Follows what is said" });
    if (SPOKEN_ONLY.indexOf(kind) > -1) return { controls: [], note: kind === "label" ? "A label shows while its point is being made, so its timing follows the speech." : "Captions follow the spoken words." };
    var mode = el("select", { "class": "field" });
    modes.forEach(function (m) { mode.appendChild(el("option", { value: m.id, text: m.label })); });
    mode.value = t.mode;
    mode.addEventListener("change", function () { t.mode = mode.value; onEdit(); renderSettings(); });
    out.push(el("label", { "class": "fld" }, ["Shows", mode].map(function (x) { return typeof x === "string" ? document.createTextNode(x) : x; })));
    if (t.mode === "first" || t.mode === "last") out.push(control(e, N("timing.seconds", t.mode === "first" ? "For how many seconds, from the start" : "For how many seconds, at the end", 0.5, 120)));
    if (t.mode === "range") { out.push(control(e, N("timing.from", "From second", 0, 3600))); out.push(control(e, N("timing.to", "To second", 1, 3600, "The end of the clip"))); }
    return { controls: out, note: t.mode === "spoken" ? "It shows while its words are being said." : "" };
  }

  function renderSettings() {
    var box = $("el-settings"), e = byId(selected);
    box.textContent = "";
    if (!e) {
      box.appendChild(el("p", { "class": "muted", text: design && design.elements.length ? "Choose an element on the picture or in Layers to change it, or add a new one." : "This format has no elements yet. Press Add element to start." }));
      return;
    }
    var schema = SCHEMA[e.type], name = el("input", { "class": "field", type: "text", maxlength: "40" });
    name.value = e.name;
    name.addEventListener("input", function () { e.name = name.value; renderLayers(); });
    box.appendChild(el("h3", { text: e.name.toLowerCase() === TYPE_LABEL[e.type].toLowerCase() ? e.name : e.name + " · " + TYPE_LABEL[e.type] }));
    box.appendChild(el("label", { "class": "fld" }, [document.createTextNode("Name"), name]));
    if (e.type === "logo" && !boot.logos.length) box.appendChild(el("p", { "class": "muted", style: "margin:0;font-size:13px", text: "Upload your logo in the Assets tab first." }));
    if (schema.text) box.appendChild(section("Text", schema.text.map(function (s) { return control(e, s); })));
    box.appendChild(section("Look", schema.look.map(function (s) { return control(e, s); })));
    var reset = el("button", { "class": "btn btn-sm", type: "button", text: "Back to the layout's own spot" });
    reset.addEventListener("click", function () { e.x = null; e.y = null; onEdit(); renderSettings(); });
    box.appendChild(section("Position and size", schema.pos.map(function (s) { return control(e, s); }).concat(e.type === "shape" ? [] : [reset]),
      "The picture is 1080 wide and 1920 tall. You can also drag the element on the picture."));
    var tm = timingControls(e);
    box.appendChild(section("Timing", tm.controls, tm.note));
    var rule = el("input", { type: "checkbox" }); rule.checked = !!e.rules.never_on_campaign; rule.disabled = BRAND.indexOf(e.type) > -1;
    rule.addEventListener("change", function () { e.rules.never_on_campaign = rule.checked; });
    box.appendChild(section("Campaign clips", [el("label", { "class": "fld inline" }, [rule, document.createTextNode("Never show this on a campaign clip")])],
      BRAND.indexOf(e.type) > -1 ? "Campaign clips never carry a handle, logo, button or progress bar, whatever is set here." : ""));
  }

  // ---- adding, ordering and removing elements ---------------------------------------------------------------------------
  function refresh() { renderLayers(); renderSettings(); renderStage(); checkPlace(false); renderBars(); previewDirty(); }
  function select(id) { if (selected === id) return; selected = id; renderLayers(); renderSettings(); renderStage(); }
  function addElement(kind, at) {   // `at`: where on the picture it was asked for (the right-click menu), else the layout's own spot
    try {
      var e = A.addElement(design.elements, boot.element_defaults, kind, { label: TYPE_LABEL[kind], at: at, logoId: boot.logos.length ? boot.logos[0].id : null });
    } catch (err) { return say(err.message, "bad"); }
    selected = e.id; say("");
    refresh();
  }
  function move(id, to) { if (A.reorder(design.elements, id, to)) refresh(); }   // to is an index in the list (0 is the back)
  function duplicate(id) {
    var twin = A.duplicate(design.elements, id, read().layout, geo);
    if (twin) { selected = twin.id; refresh(); }
  }
  function remove(id) {
    var e = byId(id);
    if (!e || e.locked || !confirm("Delete \"" + e.name + "\"? This cannot be undone.")) return;
    var r = A.remove(design.elements, id);
    if (selected === id) selected = r.next;
    refresh();
  }

  function layerButton(label, title, fn, disabled, cls) {
    var b = el("button", { type: "button", title: title, text: label, "class": cls || "" });
    b.disabled = !!disabled;
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
    return b;
  }
  function renameInline(e, button) {
    var input = el("input", { "class": "field rename", type: "text", maxlength: "40" });
    input.value = e.name;
    button.replaceWith(input); input.focus(); input.select();
    var done = false;
    function finish(keep) { if (done) return; done = true; if (keep && input.value.trim()) e.name = input.value.trim(); renderLayers(); renderSettings(); }
    input.addEventListener("blur", function () { finish(true); });
    input.addEventListener("keydown", function (ev) { if (ev.key === "Enter") finish(true); else if (ev.key === "Escape") finish(false); });
  }
  function renderLayers() {
    var ul = $("layers"), list = design ? design.elements : [], dragId = null;
    ul.textContent = "";
    list.slice().reverse().forEach(function (e) {   // the front of the picture is at the top of the list
      var i = list.indexOf(e);
      var name = el("button", { "class": "fmt-layer-name", type: "button", text: e.name, title: "Click to select, double-click to rename" });
      name.addEventListener("click", function () { select(e.id); });
      name.addEventListener("dblclick", function () { renameInline(e, name); });
      var li = el("li", { "class": "fmt-layer" + (e.id === selected ? " on" : "") + (e.visible ? "" : " hidden-el"), draggable: "true", "data-id": e.id }, [
        el("div", { "class": "fmt-layer-top" }, [el("span", { "class": "fmt-layer-grip", title: "Drag to reorder", text: "☰" }), name,
          el("span", { "class": "fmt-layer-type", text: TYPE_LABEL[e.type] })]),
        el("div", { "class": "fmt-layer-acts" }, [
          layerButton(e.visible ? "Hide" : "Show", e.visible ? "Hide it from the picture" : "Show it on the picture", function () { e.visible = !e.visible; refresh(); }),
          layerButton(e.locked ? "Unlock" : "Lock", e.locked ? "Let it be moved again" : "Stop it being moved or deleted", function () { e.locked = !e.locked; refresh(); }),
          layerButton("Copy", "Make a copy", function () { duplicate(e.id); }),
          layerButton("↑", "Bring forward", function () { move(e.id, i + 1); }, i === list.length - 1),
          layerButton("↓", "Send back", function () { move(e.id, i - 1); }, i === 0),
          layerButton("Delete", "Delete it", function () { remove(e.id); }, e.locked, "bad")])]);
      li.addEventListener("dragstart", function (ev) { dragId = e.id; ev.dataTransfer.setData("text/plain", e.id); ev.dataTransfer.effectAllowed = "move"; });
      li.addEventListener("dragover", function (ev) { ev.preventDefault(); li.classList.add("dragover"); });
      li.addEventListener("dragleave", function () { li.classList.remove("dragover"); });
      li.addEventListener("drop", function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        var id = dragId || ev.dataTransfer.getData("text/plain"), mover = byId(id);
        if (!mover || mover === e) return refresh();
        design.elements.splice(design.elements.indexOf(mover), 1);
        design.elements.splice(design.elements.indexOf(e) + 1, 0, mover);   // it lands just above the row it was dropped on
        refresh();
      });
      ul.appendChild(li);
    });
    ul.ondragover = function (ev) { ev.preventDefault(); };
    ul.ondrop = function (ev) {   // dropped below the last row: to the back
      var id = ev.dataTransfer.getData("text/plain"), mover = byId(id);
      if (mover) move(id, 0);
    };
    $("layers-empty").hidden = list.length > 0;
    [["effects", "Effects", "Small picture moves the AI proposes"], ["music", "Music", "Quiet music under the voice"]].forEach(function (f) {
      var go = el("button", { "class": "fmt-layer-name", type: "button", text: f[1], title: f[2] + ": set below the picture" });
      go.addEventListener("click", function () { jumpTo(f[0]); });
      ul.appendChild(el("li", { "class": "fmt-layer fixed" }, [el("div", { "class": "fmt-layer-top" }, [go, el("span", { "class": "fmt-layer-type", text: "For the whole format" })])]));
    });
  }
  function jumpTo(pane) {
    var target = $("pane-" + pane);
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("flash"); setTimeout(function () { target.classList.remove("flash"); }, 1400);
  }
  function renderAddMenu() {
    var box = $("add-list"); box.textContent = "";
    boot.element_types.forEach(function (t) {
      var b = el("button", { type: "button", role: "menuitem" }, [document.createTextNode(t.label), el("small", { text: BLURB[t.id] })]);
      b.addEventListener("click", function () { $("add-menu").open = false; addElement(t.id); });
      box.appendChild(b);
    });
  }

  // ---- the format-wide settings (below the picture) and the whole design ----------------------------------------------
  $("f_layout").addEventListener("change", function () { $("webcam-box").hidden = $("f_layout").value !== "stacked_webcam"; });
  function setChecked(id, v) { $(id).checked = !!v; }
  function fill(d) {
    design = copy(d);
    LEGACY_KEYS.forEach(function (k) { delete design[k]; });
    design.elements = design.elements || [];
    $("f_layout").value = d.layout; $("f_limit").value = d.crop_limit || 0;
    $("w_x").value = d.webcam.x; $("w_y").value = d.webcam.y; $("w_w").value = d.webcam.w; $("w_h").value = d.webcam.h;
    $("webcam-box").hidden = d.layout !== "stacked_webcam";
    $("m_mode").value = d.music.mode; $("m_vol").value = d.music.volume;
    $("e_intensity").value = d.effects.intensity;
    document.querySelectorAll("#e_kinds input").forEach(function (i) { i.checked = d.effects.kinds.indexOf(i.value) > -1; });
    $("s_mode").value = d.style_mode;
    document.querySelectorAll("#s_pool input").forEach(function (i) { i.checked = d.style_pool.indexOf(i.value) > -1; });
    $("pool-box").hidden = d.style_mode !== "ai";
    var first = design.elements.filter(function (e) { return e.type === "headline"; })[0] || design.elements[0];
    selected = first ? first.id : null;
    renderLayers(); renderSettings(); renderStage(); renderBars(); pv.sig = null; previewDirty(true);
  }
  function read() {
    var num = function (id) { var v = $(id).value; return v === "" ? null : Number(v); };
    var d = copy(design);
    d.layout = $("f_layout").value; d.crop_limit = num("f_limit") || 0;
    d.webcam = { x: num("w_x"), y: num("w_y"), w: num("w_w"), h: num("w_h") };
    d.music = { mode: $("m_mode").value, volume: num("m_vol") };
    d.effects = { intensity: $("e_intensity").value, kinds: Array.prototype.map.call(document.querySelectorAll("#e_kinds input:checked"), function (i) { return i.value; }) };
    d.style_mode = $("s_mode").value;
    d.style_pool = Array.prototype.map.call(document.querySelectorAll("#s_pool input:checked"), function (i) { return i.value; });
    return d;
  }
  function chosenCategories() {
    return Array.prototype.map.call(document.querySelectorAll("#cat-list input:checked"), function (i) { return i.value; });
  }

  function openBuilder(t, style) {
    current = t ? { id: t.id } : { id: null };
    var d = copy(t ? t.design : boot.defaults);
    if (style) {
      var cap = d.elements.filter(function (e) { return e.type === "captions"; })[0];
      if (!cap) { cap = copy(boot.element_defaults.captions); d.elements.push(cap); }
      cap.options.style = style; cap.visible = true;
    }
    $("f_name").value = t ? t.name : "";
    fill(d);
    document.querySelectorAll("#cat-list input").forEach(function (i) { i.checked = !!t && t.categories.indexOf(i.value) > -1; });
    $("delete").hidden = !t;
    $("builder").hidden = false; tab("templates");
    loadStage(); checkPlace(true);
    $("builder").scrollIntoView({ behavior: "smooth" });
  }

  function buildControls() {
    options($("v_video"), boot.videos.map(function (v) { return { id: v.id, label: v.title }; }));
    var pool = $("s_pool"); pool.textContent = "";
    boot.styles.forEach(function (s) { pool.appendChild(el("label", { "class": "inline", style: "display:inline-flex;gap:6px;align-items:center" },
      [el("input", { type: "checkbox", value: s.id }), document.createTextNode(s.label)])); });
    var cats = $("cat-list"); cats.textContent = "";
    boot.categories.forEach(function (c) { cats.appendChild(el("label", { "class": "inline", style: "display:inline-flex;gap:6px;align-items:center" },
      [el("input", { type: "checkbox", value: c }), document.createTextNode(c)])); });
    var kinds = $("e_kinds"); kinds.textContent = "";
    boot.effect_kinds.forEach(function (k) { kinds.appendChild(el("label", { "class": "inline", title: k.blurb, style: "display:inline-flex;gap:6px;align-items:center" },
      [el("input", { type: "checkbox", value: k.id }), document.createTextNode(k.label)])); });
    $("s_mode").addEventListener("change", function () { $("pool-box").hidden = $("s_mode").value !== "ai"; });
    renderAddMenu(); renderLogos(); renderMusic(); renderFonts();
  }

  // ---- the live stage: a paused frame with every element drawn over it -------------------------------------------------
  // Drawn in the browser so every change shows at once. The live preview below makes the exact picture; this is close.
  var fontFaces = boot.fonts.map(function (f) { return "@font-face{font-family:'f-" + f.id + "';src:url('/formats/font/" + f.id + "')}"; }).join("");
  var faceStyle = document.createElement("style"); faceStyle.textContent = fontFaces; document.head.appendChild(faceStyle);
  var GUIDE_Y = [384, 960, 1536], GUIDE_X = [540], SNAP = 14;   // the safe-zone edges and the middle, in picture units
  // What an element says on the stage: for a real clip the words that were written for it, otherwise the element's example text.
  function ghostOf(e) {
    var k = boot.clip && boot.clip.text;
    if (k) {
      if (e.text.source === "ai_headline" && k.title_lines.length) return k.title_lines.join("\n");
      if (e.text.source === "ai_point" && k.points.length) return k.points[0];
      if (e.text.source === "ai_takeaway" && k.takeaway) return k.takeaway;
    }
    return e.text.default;
  }
  var SAMPLE_WORDS = ["shut", "it", "down."].map(function (w, i) { return { text: w, t0: i, t1: i + 1 }; });

  // Where an element sits now, in the 1080 by 1920 picture: where it was put, or the layout's own spot. A headline's and a logo's
  // spot is their top (and left) edge, a progress bar's its top edge, everything else's its centre.
  function posOf(e, d) { return A.posOf(e, d.layout, geo); }
  function boxCss(e) {   // a line of text on an optional box, from the element's own style
    var s = e.style, css = "font-family:'f-" + s.font + "';font-size:" + px(s.size) + ";line-height:1.15;color:" + s.color + ";background:" + (s.box || "transparent")
      + ";padding:" + px(s.pad_y) + " " + px(s.pad_x) + ";border-radius:" + (s.radius == null ? "999px" : px(s.radius)) + ";opacity:" + (s.opacity / 100) + ";";
    if (s.uppercase) css += "text-transform:uppercase;";
    if (s.outline_w) css += "-webkit-text-stroke:" + px(s.outline_w) + " " + s.outline_color + ";paint-order:stroke fill;";
    if (s.shadow) css += "text-shadow:0 " + px(3) + " " + px(3) + " rgba(0,0,0,.7);";
    return css;
  }
  function timingTag(e) {
    var t = e.timing, words = t.mode === "first" ? "first " + t.seconds + " s" : t.mode === "last" ? "last " + t.seconds + " s"
      : t.mode === "range" && (t.from > 0 || t.to != null) ? t.from + " s to " + (t.to == null ? "the end" : t.to + " s") : "";
    return words ? el("div", { "class": "st-tag", text: words }) : null;
  }
  function renderStage() {
    var box = $("stage-items");
    box.textContent = "";
    if (!design) return;
    var d = read();
    design.elements.forEach(function (e) {
      if (!e.visible) return;
      var at = posOf(e, d), s = e.style, kids = [], t = e.type, tag = timingTag(e), place, extra = "", nodeClass = "st-item" + (e.id === selected ? " on" : "") + (e.locked ? " locked" : " movable");
      var centred = "left:" + (at.x / geo.width * 100) + "%;top:" + pct(at.y) + ";transform:translate(-50%,-50%);";
      if (t === "headline") {
        var hw = s.width_pct || 84, boxed = !!s.box, lines = String(ghostOf(e) || "").split("\n").filter(Boolean).slice(0, 2);
        if (!lines.length) lines = boot.sample.headline.slice(0, 2);
        lines.forEach(function (line) {
          kids.push(el("div", { style: "font-family:'f-" + s.font + "';font-size:" + px(s.size) + ";line-height:1.2;padding:" + px(s.size * 0.22) + " " + px(s.size * 0.45)
            + ";margin:0 0 " + px(s.size * 0.18) + ";border-radius:" + px(s.radius) + ";background:" + (boxed ? s.box : "transparent") + ";color:" + s.color + ";text-align:" + s.align
            + ";opacity:" + (s.opacity / 100) + ";text-shadow:" + (s.shadow || !boxed ? "0 " + px(3) + " " + px(3) + " rgba(0,0,0,.7)" : "none"), text: line.toUpperCase() }));
        });
        kids.push(grip());
        place = "left:" + ((100 - hw) / 2) + "%;width:" + hw + "%;top:" + pct(at.y) + ";";
      } else if (t === "captions") {
        var st = boot.styles.filter(function (x) { return x.id === e.options.style; })[0] || boot.styles[0];
        var words = stageData && stageData.words.length ? stageData.words : SAMPLE_WORDS, when = stageData && stageData.words.length ? stageData.at : 0;
        var size = s.size || st.size, upper = s.uppercase === null || s.uppercase === undefined ? st.upper : s.uppercase;
        var base = s.text_color || st.base, hi = s.highlight_color || st.accent;
        var i = words.findIndex(function (w) { return w.t0 <= when && when < w.t1; });
        if (i < 0) i = Math.max(words.findIndex(function (w) { return w.t0 >= when; }), 0);
        var from = Math.floor(i / st.group) * st.group;
        words.slice(from, from + st.group).forEach(function (w, k) {
          var spoken = from + k === i, css = "color:" + (spoken ? hi : base) + ";";
          if (spoken && st.kind === "block") css = "background:" + hi + ";color:#111;border-radius:" + px(6) + ";padding:0 " + px(8) + ";";
          kids.push(el("span", { style: css, text: (upper ? w.text.toUpperCase() : w.text) + " " }));
        });
        place = "left:0;right:0;text-align:center;transform:translateY(-50%);top:" + pct(at.y) + ";z-index:3;font-family:'f-" + (s.font || st.font) + "';font-size:" + px(size) + ";line-height:1.1;"
          + (st.kind === "plate" ? "background:rgba(0,0,0,.72);padding:" + px(14) + ";border-radius:" + px(4) + ";" : "-webkit-text-stroke:" + px(st.bord * 0.5) + " #0c0e12;paint-order:stroke fill;");
      } else if (t === "label" || t === "takeaway" || t === "button" || t === "text") {
        var line = String(ghostOf(e) || "").replace(/\n/g, " ") || TYPE_LABEL[t];
        if (t === "label" && e.options.numbered) line = "1  ·  " + line;
        kids.push(el("div", { style: boxCss(e), text: line }), grip());
        place = centred;
      } else if (t === "handle") {
        var hs = s.size || geo.defaults.handle_size;
        kids.push(el("div", { style: "font-family:'f-" + s.font + "';font-size:" + px(hs) + ";color:" + s.color + ";opacity:" + ((s.opacity != null ? s.opacity : geo.defaults.handle_alpha) / 100)
          + ";background:" + (s.box || "transparent") + ";padding:" + px(s.pad_y) + " " + px(s.pad_x) + ";border-radius:" + (s.radius == null ? 0 : px(s.radius))
          + ";" + (s.outline_w ? "-webkit-text-stroke:" + px(s.outline_w * 0.5) + " " + s.outline_color + ";paint-order:stroke fill;" : ""), text: e.text.default || "@yourhandle" }), grip());
        place = centred;
      } else if (t === "shape") {
        kids.push(el("div", { style: "width:" + px(e.w) + ";height:" + px(e.h) + ";box-sizing:border-box;background:" + s.color + ";border-radius:" + px(Math.min(s.radius, e.h / 2, e.w / 2)) + ";opacity:" + (s.opacity / 100)
          + ";" + (s.outline_w ? "border:" + px(s.outline_w) + " solid " + s.outline_color + ";" : "") }));
        kids.push(grip());
        place = centred;
      } else if (t === "logo") {
        kids.push(e.options.asset_id ? el("img", { src: "/assets/" + e.options.asset_id + "/file", alt: "", style: "display:block;width:100%;opacity:" + (s.opacity / 100) })
          : el("div", { style: "border:" + px(3) + " dashed #fff;color:#fff;font-size:" + px(30) + ";padding:" + px(16) + ";text-align:center", text: "logo" }));
        kids.push(grip());
        place = "left:" + (at.x / geo.width * 100) + "%;top:" + pct(at.y) + ";width:" + s.size + "%;";
      } else if (t === "progress_bar") {
        var track = s.track_color + Math.round(s.track_opacity * 2.55).toString(16).padStart(2, "0");
        kids.push(el("div", { style: "height:" + px(barH(e)) + ";background:" + track }, [el("div", { style: "height:100%;width:40%;background:" + s.color })]), grip());
        place = "left:0;width:100%;top:" + pct(at.y) + ";z-index:4;";
      }
      if (tag) kids.push(tag);
      box.appendChild(el("div", { "class": nodeClass, "data-id": e.id, "data-type": t, style: place + extra }, kids));
    });
    if (dragging && dragging.moved) {
      var z = A.SAFE;
      box.appendChild(el("div", { "class": "st-safe", style: "left:" + (z.left / geo.width * 100) + "%;top:" + pct(z.top) + ";width:" + ((z.right - z.left) / geo.width * 100) + "%;height:" + ((z.bottom - z.top) / geo.height * 100) + "%" }));
    }
    guides.forEach(function (g) {
      box.appendChild(el("div", { "class": "st-guide", style: g.axis === "y" ? "left:0;right:0;top:" + pct(g.v) + ";height:1px" : "top:0;bottom:0;left:" + ((g.v / geo.width) * 100) + "%;width:1px" }));
    });
  }
  function grip() { return el("div", { "class": "st-handle", "data-resize": "1" }); }

  // Snap a centre to a guide when it is close to one, and remember the guide so the stage can draw it.
  function snapCentre(centre, list, axis) {
    var best = null;
    list.forEach(function (v) { if (Math.abs(centre - v) <= SNAP && (best === null || Math.abs(centre - v) < Math.abs(centre - best))) best = v; });
    if (best === null) return centre;
    guides.push({ axis: axis, v: best });
    return best;
  }
  function moveItem(g, e, dx, dy) { A.dragTo(e, g, dx, dy, snapCentre); }
  // Dragging a corner grip sets a size: a headline's width, a logo's size, a box's width and height, the size of a line of text, a bar's thickness. The
  // same numbers the settings show.
  function resize(g, e, ev) { A.resizeTo(e, { x: (ev.clientX - g.stage.left) * g.scale, y: (ev.clientY - g.stage.top) * g.scale }, g); }
  // Elements that were put outside the safe zone, in plain words: measured from where each one is drawn.
  function safeNotes() {
    var rect = $("stage").getBoundingClientRect(), k = geo.width / rect.width, out = [];
    design.elements.forEach(function (e) {
      var node = e.visible ? document.querySelector('#stage-items [data-id="' + e.id + '"]') : null;
      if (!node) return;
      var r = node.getBoundingClientRect(), n = A.safeNote(e, { x0: (r.left - rect.left) * k, y0: (r.top - rect.top) * k, x1: (r.right - rect.left) * k, y1: (r.bottom - rect.top) * k });
      if (n) out.push(n);
    });
    return out;
  }
  // The live check: what the placement covers, in plain words. Throttled while dragging, and it always runs at the end.
  function checkPlace(force) {
    clearTimeout(checkTimer);
    if (!design) return;
    if (!force && Date.now() - checkedAt < 300) { checkTimer = setTimeout(function () { checkPlace(true); }, 300); return; }
    checkedAt = Date.now();
    var req = ++checkReq, note = $("place-note");
    post("/api/formats/check", { design: read() }).then(function (r) {
      if (req !== checkReq) return;
      if (!r.ok) { note.style.color = "#E0605A"; note.textContent = r.data.error || "That placement cannot be checked."; return; }
      var said = r.data.problems.concat(safeNotes());
      note.style.color = said.length ? "#E0605A" : "";
      note.textContent = said.length ? said[0] : "Nothing covers anything else.";
    });
  }
  $("stage-items").addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0) return;   // a right-click opens the menu, it does not start a drag
    var handle = ev.target.closest("[data-resize]"), node = ev.target.closest("[data-id]");
    var e = node ? byId(node.dataset.id) : null;
    if (!e) return;
    var stage = $("stage").getBoundingClientRect(), scale = geo.width / stage.width, at = posOf(e, read());
    var g = { id: e.id, grip: !!handle, x: ev.clientX, y: ev.clientY, at: at, stage: stage, scale: scale, w: node.offsetWidth * scale, h: node.offsetHeight * scale, moved: false,
              size: e.style && e.style.size != null ? e.style.size : geo.defaults.handle_size, halfW: node.offsetWidth * scale / 2 };
    select(e.id);
    if (e.locked) return;
    if (handle && e.type === "logo") e.x = at.x;   // pinned, so the size grows from the left edge
    dragging = g;
    ev.preventDefault();
  });
  document.addEventListener("pointermove", function (ev) {
    var g = dragging, e = g ? byId(g.id) : null;
    if (!e) return;
    var dx = (ev.clientX - g.x) * g.scale, dy = (ev.clientY - g.y) * g.scale;
    if (!g.moved && Math.abs(dx) + Math.abs(dy) < 4) return;   // a small wobble is a click, not a drag
    g.moved = true;
    guides = [];
    if (g.grip) resize(g, e, ev); else moveItem(g, e, dx, dy);
    renderStage();
    checkPlace(false);
  });
  document.addEventListener("pointerup", function () {
    var g = dragging;
    dragging = null; guides = [];
    if (g && g.moved) { renderStage(); renderSettings(); checkPlace(true); }
  });
  // ---- the right-click menu and the keyboard ------------------------------------------------------------------------------
  // The browser's own menu never shows over the picture. Right-click an element for what can be done to it, empty space to add one there.
  var menu = el("div", { "class": "ctx-menu", role: "menu", hidden: "" });
  document.body.appendChild(menu);
  function closeMenu() { menu.hidden = true; menu.textContent = ""; }
  function openMenu(x, y, title, items) {
    menu.textContent = "";
    menu.appendChild(el("div", { "class": "ctx-title", text: title }));
    items.forEach(function (it) {
      var b = el("button", { type: "button", role: "menuitem", text: it.label, "class": it.action === "delete" ? "bad" : "" });
      b.disabled = !it.enabled;
      b.addEventListener("click", function () { closeMenu(); it.go(); });
      menu.appendChild(b);
    });
    menu.hidden = false;
    menu.style.left = Math.max(4, Math.min(x, innerWidth - menu.offsetWidth - 4)) + "px";
    menu.style.top = Math.max(4, Math.min(y, innerHeight - menu.offsetHeight - 4)) + "px";
    var first = menu.querySelector("button:not([disabled])");
    if (first) first.focus();
  }
  menu.addEventListener("keydown", function (ev) {
    var items = Array.prototype.slice.call(menu.querySelectorAll("button:not([disabled])")), at = items.indexOf(document.activeElement);
    if (ev.key === "Escape") { ev.preventDefault(); closeMenu(); }
    else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); items[(at + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus(); }
  });
  document.addEventListener("pointerdown", function (ev) { if (!menu.hidden && !menu.contains(ev.target)) closeMenu(); }, true);
  window.addEventListener("blur", closeMenu); window.addEventListener("resize", closeMenu); window.addEventListener("scroll", closeMenu, true);
  function doAction(id, action) {   // one choice from an element's menu
    if (action === "delete") return remove(id);   // asks first: there is no undo
    var r = A.run(design.elements, id, action, { layout: read().layout, geo: geo });
    if (r.edit) { selected = null; select(id); var field = $("el-settings").querySelector("input"); if (field) { field.scrollIntoView({ block: "nearest" }); field.focus(); field.select(); } return; }
    if (r.selected) selected = r.selected;
    if (r.changed) refresh();
  }
  $("stage").addEventListener("contextmenu", function (ev) {
    ev.preventDefault();
    if (!design || $("stage").classList.contains("live")) return;   // playing: there are no outlines to point at
    var node = ev.target.closest("[data-id]"), e = node ? byId(node.dataset.id) : null;
    if (e) {
      select(e.id);
      openMenu(ev.clientX, ev.clientY, e.name, A.menuFor(design.elements, e.id).map(function (it) { return Object.assign({ go: function () { doAction(e.id, it.action); } }, it); }));
    } else {
      var rect = $("stage").getBoundingClientRect(), k = geo.width / rect.width, at = { x: (ev.clientX - rect.left) * k, y: (ev.clientY - rect.top) * k };
      openMenu(ev.clientX, ev.clientY, "Add element here", A.addMenu(design.elements, boot.element_types).map(function (it) { return Object.assign({ go: function () { addElement(it.kind, at); } }, it); }));
    }
  });
  document.addEventListener("keydown", function (ev) {   // Delete removes the selected element, the arrow keys move it (Shift: ten at a time)
    if (!design || $("builder").hidden || !menu.hidden || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var t = ev.target, tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
    var act = A.keyEdit(ev.key, ev.shiftKey), e = byId(selected);
    if (!act || !e) return;
    ev.preventDefault();
    if (act.kind === "delete") return remove(e.id);
    var node = document.querySelector('#stage-items [data-id="' + e.id + '"]'), k = geo.width / $("stage").getBoundingClientRect().width;
    if (A.nudge(e, act.dx, act.dy, read().layout, geo, node ? { w: node.offsetWidth * k, h: node.offsetHeight * k } : null)) { renderStage(); renderSettings(); checkPlace(false); renderBars(); previewDirty(); }
  });
  $("stage").addEventListener("pointerdown", function () { try { $("stage").focus({ preventScroll: true }); } catch (e) {} });
  function onEdit() { renderStage(); checkPlace(false); renderBars(); previewDirty(); }
  function loadStage() {
    var id = videoId(), req = ++stageReq, note = $("stage-note");
    note.style.color = "";
    if (!id) { stageData = null; $("stage-still").hidden = true; note.textContent = "Download a video to see the format on a real frame."; return renderStage(); }
    note.textContent = "Making the frame…";
    var d = read();   // only the layout, the crop limit and the webcam box change the frame; the text is drawn here
    post("/api/formats/stage", { video_id: id, clip_id: clipId(), profile: boot.profile, design: { layout: d.layout, crop_limit: d.crop_limit, webcam: d.webcam } })
      .then(function (r) {
        if (req !== stageReq) return;
        if (!r.ok) { note.style.color = "#E0605A"; note.textContent = r.data.error || "The frame could not be made."; return renderStage(); }
        stageData = r.data; $("stage-still").src = r.data.still; $("stage-still").hidden = false;
        note.textContent = "A paused frame from " + (boot.clip ? "your clip" : "the sample") + ", framed by the chosen layout, with its real words. Close to the real render, not exact."
          + (r.data.warning ? " This layout could not frame this video (" + r.data.warning + "), so the plain middle is shown." : "");
        note.style.color = r.data.warning ? "#E0A000" : "";
        renderStage();
      })
      .catch(function () { if (req === stageReq) { note.style.color = "#E0605A"; note.textContent = "The frame could not be made."; } });
  }
  var stageTimer = null;
  function reloadStageSoon() { clearTimeout(stageTimer); stageTimer = setTimeout(loadStage, 450); }
  ["f_layout", "f_limit", "w_x", "w_y", "w_w", "w_h"].forEach(function (id) { $(id).addEventListener("change", reloadStageSoon); });
  $("v_video").addEventListener("change", function () {
    loadStage();
    pause(); pv.urls = []; pv.fresh = []; pv.states = []; pv.t = 0; pv.loaded = -1;   // pictures of another video are not shown
    previewDirty(true);
  });
  $("builder").addEventListener("input", onEdit);
  $("builder").addEventListener("change", onEdit);
  $("place-reset").addEventListener("click", function () {
    design.elements.forEach(function (e) { if (!e.locked) { e.x = null; e.y = null; } });
    renderStage(); renderSettings(); checkPlace(true);
  });
  options($("f_preset"), boot.presets.map(function (p) { return { id: p.id, label: p.name }; }), "Keep the settings above");
  $("f_preset").addEventListener("change", function () {
    var p = boot.presets.filter(function (x) { return x.id === $("f_preset").value; })[0];
    $("f_preset").value = "";
    if (p) { fill(copy(p.design)); loadStage(); say("The " + p.name + " settings are in the builder. Save to keep them.", "good"); }
  });


  // ---- the live preview: the real renderer's pictures, in pieces, always for the design as it is now -----------------------
  // Every change starts a round on the server (a moment after you stop). The piece under the playhead is made first, then its
  // neighbours, then the rest; a piece that did not change is reused. Two video elements take turns so playback is continuous.
  var pv = { windows: {}, round: 0, token: 0, urls: [], fresh: [], states: [], spans: [], length: 35, playing: false, t: 0, loaded: -1, queuedNext: -1,
             live: 0, sig: null, startTimer: null, pollTimer: null, scrub: false, error: "", waiting: false, pending: false, raf: 0 };
  var vids = [$("pv-a"), $("pv-b")];
  function clock(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }
  function segAt(t) {
    for (var i = pv.spans.length - 1; i >= 0; i--) if (t >= pv.spans[i][0]) return i;
    return 0;
  }
  function hasVideo() { return !!boot.clip || !!$("v_video").value; }   // Edit look has the clip's own video; a new format needs a sample
  function videoId() { return boot.clip ? boot.clip.video_id : $("v_video").value; }
  function clipId() { return boot.clip ? boot.clip.id : undefined; }
  function setStatus(text, bad) { var n = $("pv-status"); n.textContent = text; n.style.color = bad ? "#E0605A" : ""; }

  // The preview is out of date from the moment the design changes until the server says each piece is ready again.
  function previewDirty(force) {
    if (!design || !hasVideo()) return;
    var sig = JSON.stringify([read(), videoId(), $("pv-draft").checked]);
    if (sig === pv.sig && !force) return;
    pv.sig = sig; pv.pending = true; pv.error = "";
    pv.fresh = pv.fresh.map(function () { return false; });   // the old pictures stay on screen, but are not played as if they were current
    clearTimeout(pv.startTimer);
    pv.startTimer = setTimeout(startRound, force ? 0 : 450);
    paintPreview();
  }
  function startRound() {
    clearTimeout(pv.startTimer); pv.startTimer = null;
    if (!design || !hasVideo() || $("builder").hidden) return;
    var token = ++pv.token;
    post("/api/formats/preview/start", { design: read(), video_id: videoId(), clip_id: clipId(), playhead: pv.t, profile: boot.profile, draft: $("pv-draft").checked })
      .then(function (r) {
        if (token !== pv.token) return;   // a newer edit already asked again
        pv.pending = false;
        if (!r.ok) { pv.error = r.data.error || "The preview could not be made."; pv.fresh = pv.fresh.map(function () { return false; }); return paintPreview(); }
        pv.round = r.data.round; applyStatus(r.data);
      })
      .catch(function () { if (token === pv.token) { pv.pending = false; pv.error = "The preview could not be made."; paintPreview(); } });
  }
  function applyStatus(s) {
    pv.spans = s.segments.map(function (g) { return [g.start, g.end]; }); pv.length = s.length; pv.windows = s.elements || {};
    s.segments.forEach(function (g, i) {
      pv.states[i] = g.state;
      if (g.state === "ready") { pv.urls[i] = g.url; pv.fresh[i] = true; } else pv.fresh[i] = false;
      if (g.state === "failed") pv.error = g.error || "A part of the preview could not be made.";
    });
    $("pv-seek").max = Math.round(s.length * 100);
    renderBars();
    paintPreview();
    clearTimeout(pv.pollTimer);
    if (!s.done) pv.pollTimer = setTimeout(pollRound, 350);
    if (pv.playing || pv.scrub) drive();
  }
  function pollRound() {
    var round = pv.round;
    fetch("/api/formats/preview/status?round=" + round).then(function (r) { return r.json(); }).then(function (s) {
      if (round !== pv.round) return;
      if (s.superseded) return;   // the next start answers for the newest round
      applyStatus(s);
    }).catch(function () { pv.pollTimer = setTimeout(pollRound, 1000); });
  }

  // ---- drawing the player's state -------------------------------------------------------------------------------------
  function paintPreview() {
    var here = segAt(pv.t), behind = pv.pending || pv.fresh.some(function (f, i) { return !f && (i === here || (pv.playing && i === here + 1)); });
    $("pv-badge").hidden = !(behind && !pv.error && hasVideo() && !pv.waiting);
    var strip = $("pv-ready"); strip.textContent = "";
    pv.spans.forEach(function (sp, i) {
      var cls = pv.fresh[i] ? "ready" : pv.states[i] === "failed" ? "failed" : pv.states[i] === "rendering" ? "rendering" : "queued";
      strip.appendChild(el("span", { "class": cls, style: "width:" + ((sp[1] - sp[0]) / pv.length * 100) + "%", title: clock(sp[0]) + " to " + clock(sp[1]) + ": " + (cls === "ready" ? "ready" : cls === "failed" ? "could not be made" : cls === "rendering" ? "being made" : "waiting") }));
    });
    var done = pv.fresh.filter(Boolean).length;
    if (!hasVideo()) setStatus("Download a video to see a real preview.");
    else if (pv.error) setStatus(pv.error, true);
    else if (pv.waiting) setStatus("Waiting for this part to finish being made…");
    else if (pv.pending || (pv.spans.length && done < pv.spans.length)) setStatus("Making the picture: " + done + " of " + (pv.spans.length || "?") + " parts ready. You can press play now.");
    else setStatus("Up to date: this is the real picture.");
    $("pv-play").textContent = pv.playing ? "⏸ Pause" : "▶ Play";
    $("pv-play").disabled = !hasVideo();
    $("pv-time").textContent = clock(pv.t) + " / " + clock(pv.length);
    if (!pv.scrub) $("pv-seek").value = Math.round(pv.t * 100);
    var live = vids.some(function (v) { return !v.hidden; });
    $("stage").classList.toggle("live", live);
    $("pv-mode").textContent = live ? "Live: the real picture" : "Ghost: every element";
  }
  function paintTime() {
    $("pv-time").textContent = clock(pv.t) + " / " + clock(pv.length);
    if (!pv.scrub) $("pv-seek").value = Math.round(pv.t * 100);
  }
  function renderBars() {   // when each element is on screen, as thin bars under the timeline
    var box = $("pv-bars"); box.textContent = "";
    if (!design) return;
    design.elements.slice().reverse().forEach(function (e) {
      if (!e.visible) return;
      var real = pv.windows[e.id], marks = [];   // (from, to, how it is known) in seconds
      if (real && real.length) real.forEach(function (w) { marks.push(w ? [w[0], w[1], ""] : [0, pv.length, ""]); });
      else {   // before the first answer from the server: what the timing setting says, shown fainter
        var t = e.timing, a = 0, b = pv.length;
        if (t.mode === "first") b = Math.min(t.seconds, pv.length);
        else if (t.mode === "last") a = Math.max(pv.length - t.seconds, 0);
        else if (t.mode === "range") { a = Math.min(t.from, pv.length); b = t.to == null ? pv.length : Math.min(t.to, pv.length); }
        marks.push([a, b, "guess"]);
      }
      var spoken = e.timing.mode === "spoken";
      box.appendChild(el("div", { "class": "bar" }, marks.map(function (m) {
        return el("span", { "class": (spoken ? "spoken " : "") + m[2], title: e.name + ": " + clock(m[0]) + " to " + clock(m[1]) + (spoken ? " (follows what is said)" : ""),
          style: "left:" + (m[0] / pv.length * 100) + "%;width:" + Math.max((m[1] - m[0]) / pv.length * 100, 0.5) + "%" });
      })));
    });
  }

  // ---- playing ---------------------------------------------------------------------------------------------------------
  function load(v, i, at) {   // put piece i in this video element, at this many seconds into the piece
    var url = pv.urls[i];
    if (v.dataset.url === url && v.readyState >= 1) { v.currentTime = at; return; }
    v.dataset.index = i; v.dataset.url = url;
    v.src = url;
    var go = function () { v.currentTime = at; v.removeEventListener("loadedmetadata", go); };
    v.addEventListener("loadedmetadata", go);
  }
  function show(i) {   // the element carrying piece i on screen; the other one waits underneath
    vids.forEach(function (v, k) { v.hidden = k !== pv.live; v.classList.toggle("top", k === pv.live); });
  }
  // One step of whatever is wanted: find the piece for the playhead, wait for it if it is not made, and keep the next one ready.
  function drive() {
    clearTimeout(pv.raf);
    var i = segAt(pv.t), v = vids[pv.live], span = pv.spans[i];
    if (!span) return;
    var usable = pv.playing ? pv.fresh[i] : !!pv.urls[i];   // playing needs the current picture; scrubbing may show the old one
    if (!usable) {
      v.pause(); pv.waiting = pv.playing && !pv.error; pv.loaded = -1;
      if (!pv.scrub && !pv.playing) { v.hidden = true; }   // playing: the last picture stays up until the new one is ready
      paintPreview();
      return;
    }
    pv.waiting = false;
    if (pv.loaded !== i || v.dataset.url !== pv.urls[i]) {
      pv.loaded = i; load(v, i, Math.max(pv.t - span[0], 0)); show(i);
      if (pv.playing) { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
    }
    if (!pv.scrub && pv.playing) {
      var other = vids[1 - pv.live], n = i + 1;
      if (n < pv.spans.length && pv.fresh[n] && pv.queuedNext !== n) { pv.queuedNext = n; load(other, n, 0); other.pause(); }
      if (v.paused && !v.ended) { var q = v.play(); if (q && q.catch) q.catch(function () {}); }
      pv.t = Math.min(span[0] + v.currentTime, span[1]);
      var finished = v.ended || (v.duration && v.currentTime >= v.duration - 0.05);
      if (finished) {
        if (n >= pv.spans.length) { pv.playing = false; pv.t = pv.length; v.pause(); setStatus("The end."); }
        else {
          pv.t = pv.spans[n][0];
          if (pv.fresh[n] && pv.queuedNext === n) {   // the piece after is ready and waiting: swap, so playback carries on without a gap
            pv.live = 1 - pv.live; pv.loaded = n; pv.queuedNext = -1; show(n);
            var np = vids[pv.live].play(); if (np && np.catch) np.catch(function () {});
          } else { v.pause(); pv.loaded = -1; pv.waiting = true; paintPreview(); }   // not made yet: hold the last picture until it is
        }
      }
    }
    paintTime();
    if (pv.playing) pv.raf = setTimeout(drive, 50);
  }
  function play() {
    if (!hasVideo()) return;
    if (pv.t >= pv.length - 0.05) pv.t = 0;
    if (pv.startTimer) startRound();   // an edit is waiting to be sent: send it now, the picture must be the latest
    pv.playing = true; pv.scrub = false; pv.loaded = -1; pv.queuedNext = -1;
    drive(); paintPreview();
  }
  function pause() {
    pv.playing = false; pv.waiting = false;
    vids.forEach(function (v) { v.pause(); v.hidden = true; });   // paused: the stage shows every element so each can be picked and moved
    clearTimeout(pv.raf); paintPreview();
  }
  vids.forEach(function (v) { v.addEventListener("ended", function () { if (pv.playing && v === vids[pv.live]) drive(); }); });   // the swap should not wait for the next tick
  $("pv-play").addEventListener("click", function () { if (pv.playing) pause(); else play(); });
  $("pv-mute").addEventListener("click", function () {
    var off = $("pv-mute").getAttribute("aria-pressed") !== "true";
    $("pv-mute").setAttribute("aria-pressed", off ? "true" : "false"); $("pv-mute").textContent = off ? "Sound off" : "Sound on";
    vids.forEach(function (v) { v.muted = off; });
  });
  // The timeline: drag to any moment. The nearest picture shows at once (the old one if the new one is not made yet) and that
  // spot is made first.
  var seekTimer = null;
  $("pv-seek").addEventListener("input", function () {
    pv.t = Number($("pv-seek").value) / 100; pv.scrub = true; pv.loaded = -1; pv.queuedNext = -1; pv.waiting = false;
    vids.forEach(function (v) { v.pause(); });
    var i = segAt(pv.t);
    if (!pv.urls[i]) { vids.forEach(function (v) { v.hidden = true; }); }
    drive(); paintPreview();
    clearTimeout(seekTimer); seekTimer = setTimeout(startRound, 120);   // ask for this spot first
  });
  $("pv-seek").addEventListener("change", function () {
    pv.scrub = false; pv.loaded = -1; pv.queuedNext = -1;
    if (pv.playing) drive(); else { vids.forEach(function (v) { v.pause(); v.hidden = true; }); paintPreview(); }
  });
  $("pv-draft").addEventListener("change", function () { previewDirty(true); });

  buildControls(); renderPresets(); renderStyles(); renderTemplates();
  try { var n = sessionStorage.getItem("fmt-note"); if (n) { say(n, "good"); sessionStorage.removeItem("fmt-note"); } } catch (e) {}
  tab((location.hash || "").replace("#", "") || (boot.templates.length ? "templates" : "presets"));
  if (boot.clip_problem) say(boot.clip_problem, "bad");   // a clip that cannot be edited gets no builder, only the reason
  if (boot.clip) {
    var own = boot.templates.filter(function (t) { return t.id === boot.clip.template_id; })[0];
    openBuilder(own || null);
    if (boot.clip.design) fill(copy(boot.clip.design));
    var src = boot.clip.source_template;
    $("f_name").value = src ? src.name : "A new format";
    ["save", "save-copy", "delete", "close"].forEach(function (id) { $(id).hidden = true; });
    $("v_video").closest("label").hidden = true;   // the clip's own video is the sample
    $("clip-update").hidden = !src;
    if (src) $("clip-update").textContent = "Update \"" + src.name + "\" format";
    var notes = [];
    if (!boot.clip.text) notes.push("This clip was made before its text was kept, so the picture shows each element's example text.");
    if (boot.clip.campaign) notes.push("It is a campaign clip: the handle, logo, button and progress bar are never shown on it.");
    if (boot.clip.waiting_post) notes.push("Its post keeps its place in the queue and sends the new file.");
    $("clip-notes").textContent = notes.join(" ");
    $("clip-box").hidden = false; $("clip-box").querySelector("div").textContent = "You are editing the look of your real clip (clip " + boot.clip.id + "). The picture is the clip itself.";
    $("clip-actions").hidden = false;
  }
})();
