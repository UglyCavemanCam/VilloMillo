/* The format builder's edits, as plain functions on the list of elements: adding, copying, deleting and reordering elements, the right-click menu, dragging,
   resizing and nudging on the 1080 by 1920 picture, and the safe zone. They touch no page, so they run the same in a browser and in node, where the tests
   exercise every one of them. formats-builder.js draws the page and calls these. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api; else root.FormatsActions = api;
})(typeof self !== "undefined" ? self : this, function () {
  var W = 1080, H = 1920;
  // Where the app's own title, buttons and caption would cover the picture: text and pictures are best kept inside these lines.
  var SAFE = { left: 60, right: 1020, top: 150, bottom: 1560 };
  var GUIDES = { x: [540], y: [384, 960, 1536] };      // the lines a dragged element snaps to: the safe-zone edges and the middle
  var DEFAULTS = { bar_height: 14, logo_margin: 60 };   // what the recipe uses where an element leaves it open; configure() sets the app's own numbers
  var NO_X = ["headline", "captions", "progress_bar"];   // an element of these kinds is placed by its height only
  var BOX_TEXT = ["label", "takeaway", "button", "text"];
  var RESIZABLE = ["headline", "logo", "shape", "label", "takeaway", "button", "text", "handle", "progress_bar"];

  function clamp(v, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, v))); }
  function copy(x) { return JSON.parse(JSON.stringify(x)); }
  function find(list, id) { for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i]; return null; }
  function configure(d) { for (var k in d) DEFAULTS[k] = d[k]; }
  function barHeight(e) { return e.style.height != null ? e.style.height : DEFAULTS.bar_height; }
  function usesX(e) { return NO_X.indexOf(e.type) < 0; }
  function resizable(e) { return RESIZABLE.indexOf(e.type) > -1; }

  // Where an element sits now, in the picture: where it was put, or the layout's own spot. A headline's and a logo's spot is their top
  // (and left) edge, a progress bar's its top edge, everything else's its centre. `geo` is the page's geometry (the layout's own spots).
  function posOf(e, layout, geo) {
    var t = e.type;
    if (t === "headline") return { x: 0, y: e.y != null ? e.y : geo.headline_top[layout] };
    if (t === "captions") return { x: W / 2, y: e.y != null ? e.y : geo.positions[e.options.position] };
    if (t === "progress_bar") return { x: 0, y: e.y != null ? e.y : H - barHeight(e) };
    if (t === "logo") {
      var w = W * (e.style.size || 14) / 100, margin = e.options.margin != null ? e.options.margin : DEFAULTS.logo_margin;
      var right = layout === "stacked_webcam" || layout === "split" || e.options.corner === "right";
      return { x: e.x != null ? e.x : (right ? W - w - margin : margin), y: e.y != null ? e.y : geo.logo_y[layout] };
    }
    var row = t === "label" || t === "takeaway" || t === "button" ? geo.chips_y[layout] : H / 2;
    return { x: e.x != null ? e.x : W / 2, y: e.y != null ? e.y : row };
  }

  // ---- adding, copying, deleting, ordering ----------------------------------------------------------------------------
  function nextId(list, kind) {
    var taken = list.map(function (e) { return e.id; }), id = kind, n = 1;
    while (taken.indexOf(id) > -1) { n += 1; id = kind + "-" + n; }
    return { id: id, n: n };
  }
  function place(e, at) {   // put a new element where it was asked for: its centre, or the edge a headline, logo or bar is measured from
    if (e.type === "headline" || e.type === "captions") e.y = clamp(at.y, 0, H);
    else if (e.type === "progress_bar") e.y = clamp(at.y, 0, H - barHeight(e));
    else { e.x = clamp(at.x, 0, W); e.y = clamp(at.y, 0, H); }
  }
  // A new element with its usual look. `o`: label (its name), at (where on the picture), logoId (the picture a logo shows). Throws a plain-words Error.
  function addElement(list, defaults, kind, o) {
    o = o || {};
    if (kind === "captions" && list.some(function (x) { return x.type === "captions"; })) throw new Error("A format has one set of captions. Change the ones it has.");
    if (kind === "logo" && o.logoId == null) throw new Error("Upload your logo in the Assets tab first, then add it here.");
    var e = copy(defaults[kind]), made = nextId(list, kind);
    if (kind === "logo") e.options.asset_id = o.logoId;
    e.id = made.id; e.name = (o.label || kind) + (made.n > 1 ? " " + made.n : "");
    if (o.at) place(e, o.at);
    list.push(e);
    return e;
  }
  function duplicate(list, id, layout, geo) {   // the copy sits just in front of the original, a little down and across so it can be told apart
    var e = find(list, id);
    if (!e) return null;
    var twin = copy(e), at = posOf(e, layout, geo), made = nextId(list, e.type);
    twin.id = made.id; twin.name = e.name + " copy"; twin.locked = false;
    if (e.type !== "progress_bar") twin.y = clamp(at.y + 60, 0, H);
    if (usesX(e)) twin.x = clamp(at.x + 40, 0, W);
    list.splice(list.indexOf(e) + 1, 0, twin);
    return twin;
  }
  function remove(list, id) {   // a locked element is not deleted. Returns {removed, next}: the element to select afterwards
    var e = find(list, id);
    if (!e || e.locked) return { removed: false, next: id };
    var at = list.indexOf(e);
    list.splice(at, 1);
    var near = list[Math.min(at, list.length - 1)];
    return { removed: true, next: near ? near.id : null };
  }
  function reorder(list, id, to) {   // to is an index in the list (0 is the back)
    var from = list.map(function (e) { return e.id; }).indexOf(id);
    if (from < 0 || to < 0 || to >= list.length || to === from) return false;
    list.splice(to, 0, list.splice(from, 1)[0]);
    return true;
  }
  function step(list, id, by) { var i = list.map(function (e) { return e.id; }).indexOf(id); return i < 0 ? false : reorder(list, id, i + by); }
  function toggle(list, id, field) { var e = find(list, id); if (!e) return false; e[field] = !e[field]; return true; }
  function resetPosition(list, id) { var e = find(list, id); if (!e || e.locked) return false; e.x = null; e.y = null; return true; }

  // ---- the right-click menu ------------------------------------------------------------------------------------------------
  // The menu for one element: what is offered, and what is possible right now (a locked element is not moved, reset or deleted).
  function menuFor(list, id) {
    var e = find(list, id), i = list.indexOf(e);
    if (!e) return [];
    return [
      { action: "edit", label: "Edit", enabled: true },
      { action: "duplicate", label: "Duplicate", enabled: true },
      { action: "hide", label: e.visible ? "Hide" : "Show", enabled: true },
      { action: "lock", label: e.locked ? "Unlock" : "Lock", enabled: true },
      { action: "forward", label: "Bring forward", enabled: i < list.length - 1 },
      { action: "back", label: "Send back", enabled: i > 0 },
      { action: "reset", label: "Reset position", enabled: !e.locked && (e.x != null || e.y != null) },
      { action: "delete", label: "Delete", enabled: !e.locked }
    ];
  }
  // The menu for empty space: one entry per kind of element that can be added here.
  function addMenu(list, types) {
    return types.map(function (t) { return { action: "add", kind: t.id, label: t.label, enabled: !(t.id === "captions" && list.some(function (x) { return x.type === "captions"; })) }; });
  }
  // Do one menu action. `ctx`: layout and geo (for a copy's place). Returns {changed, selected, edit}: edit means "open its settings".
  function run(list, id, action, ctx) {
    var out = { changed: false, selected: id, edit: false };
    if (action === "edit") out.edit = true;
    else if (action === "duplicate") { var twin = duplicate(list, id, ctx.layout, ctx.geo); out.changed = !!twin; if (twin) out.selected = twin.id; }
    else if (action === "hide") out.changed = toggle(list, id, "visible");
    else if (action === "lock") out.changed = toggle(list, id, "locked");
    else if (action === "forward") out.changed = step(list, id, 1);
    else if (action === "back") out.changed = step(list, id, -1);
    else if (action === "reset") out.changed = resetPosition(list, id);
    else if (action === "delete") { var r = remove(list, id); out.changed = r.removed; out.selected = r.next; }
    return out;
  }

  // ---- dragging, resizing, nudging ------------------------------------------------------------------------------------------
  // Move an element by (dx, dy) picture units from where it was picked up. `g`: at (where it was, from posOf) and w, h (its size on the picture).
  // `snap(centre, lines, axis)` may pull a centre to a guide line. The whole element is kept inside the picture.
  function dragTo(e, g, dx, dy, snap) {
    snap = snap || function (c) { return c; };
    var w = g.w || 0, h = g.h || 0, at = g.at;
    if (e.type === "logo") {
      e.x = clamp(snap(at.x + dx + w / 2, GUIDES.x, "x") - w / 2, 0, W - w);
      e.y = clamp(snap(at.y + dy + h / 2, GUIDES.y, "y") - h / 2, 0, H - h);
    } else if (e.type === "headline") {
      e.y = clamp(snap(at.y + dy + h / 2, GUIDES.y, "y") - h / 2, 0, H - h);
    } else if (e.type === "progress_bar") {
      e.y = clamp(at.y + dy, 0, H - barHeight(e));
    } else if (e.type === "captions") {
      e.y = clamp(snap(at.y + dy, GUIDES.y, "y"), h / 2, H - h / 2);
    } else {
      e.x = clamp(snap(at.x + dx, GUIDES.x, "x"), w / 2, W - w / 2);
      e.y = clamp(snap(at.y + dy, GUIDES.y, "y"), h / 2, H - h / 2);
    }
  }
  // Drag a corner grip to (p.x, p.y) on the picture: it sets the same number the settings show. `start`: at (the element's place when picked up),
  // size (its text size then) and halfW (half its width then), which text sizes grow from. Elements without a size to set are left alone.
  function resizeTo(e, p, start) {
    var t = e.type, at = start.at;
    if (t === "headline") e.style.width_pct = clamp(Math.abs(p.x - W / 2) * 2 / W * 100, 40, 100);
    else if (t === "logo") e.style.size = clamp((p.x - at.x) / W * 100, 6, 30);
    else if (t === "shape") { e.w = clamp(Math.abs(p.x - at.x) * 2, 4, W); e.h = clamp(Math.abs(p.y - at.y) * 2, 4, H); }
    else if (t === "progress_bar") e.style.height = clamp(p.y - at.y, 2, 80);
    else if (BOX_TEXT.indexOf(t) > -1) e.style.size = clamp(start.size * Math.abs(p.x - at.x) / Math.max(start.halfW, 1), 16, 160);
    else if (t === "handle") e.style.size = clamp(start.size * Math.abs(p.x - at.x) / Math.max(start.halfW, 1), 12, 120);
  }
  // The keyboard: Delete removes the selected element, the arrow keys move it by one picture unit (ten with Shift).
  function keyEdit(key, shift) {
    var n = shift ? 10 : 1;
    if (key === "Delete") return { kind: "delete" };
    if (key === "ArrowLeft") return { kind: "nudge", dx: -n, dy: 0 };
    if (key === "ArrowRight") return { kind: "nudge", dx: n, dy: 0 };
    if (key === "ArrowUp") return { kind: "nudge", dx: 0, dy: -n };
    if (key === "ArrowDown") return { kind: "nudge", dx: 0, dy: n };
    return null;
  }
  function nudge(e, dx, dy, layout, geo, size) {
    if (e.locked) return false;
    dragTo(e, { at: posOf(e, layout, geo), w: size ? size.w : 0, h: size ? size.h : 0 }, dx, dy);
    return true;
  }

  // ---- the safe zone ---------------------------------------------------------------------------------------------------------
  // Plain words about an element someone put outside the safe zone (its box on the picture), or "". Only an element that was moved is judged: the
  // layout's own spots are chosen to be safe, and a progress bar belongs at the very edge.
  function safeNote(e, box) {
    if (e.type === "progress_bar" || (e.x == null && e.y == null)) return "";
    if (box.x0 >= SAFE.left && box.x1 <= SAFE.right && box.y0 >= SAFE.top && box.y1 <= SAFE.bottom) return "";
    return "The " + e.name.toLowerCase() + " sits outside the safe zone (the dashed lines): the app's own title and buttons can cover part of it.";
  }

  return { configure: configure, W: W, H: H, SAFE: SAFE, GUIDES: GUIDES, clamp: clamp, find: find, posOf: posOf, usesX: usesX, resizable: resizable, barHeight: barHeight, nextId: nextId,
           addElement: addElement, duplicate: duplicate, remove: remove, reorder: reorder, step: step, toggle: toggle, resetPosition: resetPosition,
           menuFor: menuFor, addMenu: addMenu, run: run, dragTo: dragTo, resizeTo: resizeTo, keyEdit: keyEdit, nudge: nudge, safeNote: safeNote };
});
