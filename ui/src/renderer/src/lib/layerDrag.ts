/** Moving and resizing a layer on the Short (a facecam, the Game UI), with
 *  snapping to guide lines the way a design editor does it: the layer's edges
 *  and middle snap to the Short's middle and edges, the platform's safe lines,
 *  and the other layers' edges and middles. Pure maths, in canvas px, so it can
 *  be tested on its own (tests/test_ui_layer_snapping.py). */

export type Box = [number, number, number, number]
export type Handle = 'move' | 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
export interface Targets {
  xs: number[]
  ys: number[]
}
export interface DragOptions {
  /** width / height to keep (a facecam), or null for a free box (the Game UI). */
  aspect: number | null
  canvas: [number, number]
  minW: number
  minH: number
  /** Lines to snap to, or null with snapping off (Alt held). */
  targets: Targets | null
  /** How close counts as lined up, canvas px. */
  tol: number
}
export interface DragResult {
  box: Box
  /** The lines it snapped to, to draw while dragging. */
  guides: Targets
}

function nearest(values: number[], targets: number[], tol: number): { delta: number; line: number } | null {
  let best: { delta: number; line: number } | null = null
  for (const v of values)
    for (const t of targets) {
      const d = t - v
      if (Math.abs(d) <= tol && (best === null || Math.abs(d) < Math.abs(best.delta))) best = { delta: d, line: t }
    }
  return best
}

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v))

export function dragLayer(start: Box, handle: Handle, dx: number, dy: number, o: DragOptions): DragResult {
  const [W, H] = o.canvas
  const [x0, y0, w0, h0] = start
  const guides: Targets = { xs: [], ys: [] }

  if (handle === 'move') {
    let x = x0 + dx
    let y = y0 + dy
    if (o.targets) {
      const sx = nearest([x, x + w0 / 2, x + w0], o.targets.xs, o.tol)
      if (sx) {
        x += sx.delta
        guides.xs.push(sx.line)
      }
      const sy = nearest([y, y + h0 / 2, y + h0], o.targets.ys, o.tol)
      if (sy) {
        y += sy.delta
        guides.ys.push(sy.line)
      }
    }
    return { box: [clamp(x, 0, W - w0), clamp(y, 0, H - h0), w0, h0], guides }
  }

  const west = handle.includes('w')
  const east = handle.includes('e')
  const north = handle.includes('n')
  const south = handle.includes('s')

  if (o.aspect === null) {
    // A free box: each moving edge follows the pointer and snaps on its own.
    let L = x0
    let R = x0 + w0
    let T = y0
    let B = y0 + h0
    const snapEdge = (v: number, axis: 'xs' | 'ys'): number => {
      if (!o.targets) return v
      const s = nearest([v], o.targets[axis], o.tol)
      if (!s) return v
      guides[axis].push(s.line)
      return v + s.delta
    }
    if (west) L = snapEdge(clamp(x0 + dx, 0, R - o.minW), 'xs')
    if (east) R = snapEdge(clamp(x0 + w0 + dx, L + o.minW, W), 'xs')
    if (north) T = snapEdge(clamp(y0 + dy, 0, B - o.minH), 'ys')
    if (south) B = snapEdge(clamp(y0 + h0 + dy, T + o.minH, H), 'ys')
    return { box: [L, T, R - L, B - T], guides }
  }

  // A facecam keeps its shape: the edge (or the corner's stronger direction)
  // being pulled decides the size, and the opposite side stays put.
  const a = o.aspect
  const wx = west ? w0 - dx : east ? w0 + dx : w0
  const hy = north ? h0 - dy : south ? h0 + dy : h0
  const horizontal = (west || east) && (!(north || south) || Math.abs(wx / w0 - 1) >= Math.abs(hy / h0 - 1))
  let w = horizontal ? wx : hy * a
  if (o.targets) {
    if (horizontal) {
      const edge = west ? x0 + w0 - w : x0 + w
      const s = nearest([edge], o.targets.xs, o.tol)
      if (s) {
        w += west ? -s.delta : s.delta
        guides.xs.push(s.line)
      }
    } else {
      const h = w / a
      const edge = north ? y0 + h0 - h : y0 + h
      const s = nearest([edge], o.targets.ys, o.tol)
      if (s) {
        w = (h + (north ? -s.delta : s.delta)) * a
        guides.ys.push(s.line)
      }
    }
  }
  w = clamp(w, o.minW, Math.min(W, H * a))
  const h = w / a
  // Where it grows from: the opposite corner, or the opposite edge's middle.
  const x = west ? x0 + w0 - w : east ? x0 : x0 + (w0 - w) / 2
  const y = north ? y0 + h0 - h : south ? y0 : y0 + (h0 - h) / 2
  return { box: [clamp(x, 0, W - w), clamp(y, 0, H - h), w, h], guides }
}

const overlaps = (a: Box, b: Box): boolean =>
  a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3]

/** A box placed on the video frame that keeps clear of `walls` (for the game
 *  box: the webcams, a little past their border): one that started clear
 *  stops at a wall's edge instead of going over it, so the webcam doesn't show
 *  in the game too. Moved, it goes to the nearest side of the wall that fits
 *  the frame; resized, the edge being pulled stops at the wall (and with
 *  `aspect`, the other side follows so the box keeps its shape). A box already
 *  over a wall isn't held by it, and when nothing fits the box goes where it
 *  was put. `next` is where dragLayer put it; the wall edges it stopped at come
 *  back as guides, to draw. */
export function keepOut(
  start: Box,
  handle: Handle,
  next: Box,
  walls: Box[],
  canvas: [number, number],
  aspect: number | null = null
): DragResult {
  const [W, H] = canvas
  const guides: Targets = { xs: [], ys: [] }
  const holding = walls.filter((w) => !overlaps(start, w))
  let box = next
  for (const [wx, wy, ww, wh] of holding) {
    if (!overlaps(box, [wx, wy, ww, wh])) continue
    const [x, y, w, h] = box
    const options: { box: Box; line: number; axis: 'xs' | 'ys' }[] = []
    if (handle === 'move') {
      options.push(
        { box: [wx - w, y, w, h], line: wx, axis: 'xs' },
        { box: [wx + ww, y, w, h], line: wx + ww, axis: 'xs' },
        { box: [x, wy - h, w, h], line: wy, axis: 'ys' },
        { box: [x, wy + wh, w, h], line: wy + wh, axis: 'ys' }
      )
    } else {
      const [sx, sy, sw, sh] = start
      if (handle.includes('e') && sx + sw <= wx) options.push({ box: [x, y, wx - x, h], line: wx, axis: 'xs' })
      if (handle.includes('w') && sx >= wx + ww)
        options.push({ box: [wx + ww, y, x + w - (wx + ww), h], line: wx + ww, axis: 'xs' })
      if (handle.includes('s') && sy + sh <= wy) options.push({ box: [x, y, w, wy - y], line: wy, axis: 'ys' })
      if (handle.includes('n') && sy >= wy + wh)
        options.push({ box: [x, wy + wh, w, y + h - (wy + wh)], line: wy + wh, axis: 'ys' })
      if (aspect) for (const o of options) o.box = keepShape(o.box, box, handle, aspect)
    }
    const fits = options.filter(
      (o) =>
        o.box[2] > 0 &&
        o.box[3] > 0 &&
        o.box[0] >= 0 &&
        o.box[1] >= 0 &&
        o.box[0] + o.box[2] <= W &&
        o.box[1] + o.box[3] <= H &&
        !holding.some((other) => overlaps(o.box, other))
    )
    if (fits.length === 0) continue
    // Moved: the least distance from where it was put. Resized: the most of it kept.
    const cost = (o: (typeof fits)[number]): number =>
      handle === 'move' ? Math.abs(o.box[0] - x) + Math.abs(o.box[1] - y) : -(o.box[2] * o.box[3])
    const best = fits.reduce((a, b) => (cost(b) < cost(a) ? b : a))
    box = best.box
    guides[best.axis].push(best.line)
  }
  return { box, guides }
}

/** A resized box whose width or height a wall cut short, given back its shape:
 *  the other side follows, from the edge the handle isn't pulling (the middle
 *  for a side handle), the way dragLayer grows a facecam. */
function keepShape(cut: Box, pulled: Box, handle: Handle, aspect: number): Box {
  const [x, y, w, h] = cut
  if (w !== pulled[2]) {
    const nh = w / aspect
    const ny = handle.includes('n') ? y + h - nh : handle.includes('s') ? y : y + (h - nh) / 2
    return [x, ny, w, nh]
  }
  const nw = h * aspect
  const nx = handle.includes('w') ? x + w - nw : handle.includes('e') ? x : x + (w - nw) / 2
  return [nx, y, nw, h]
}

/** A box given another shape (width / height) with its middle and its area
 *  kept, inside the canvas: the frame editor's game box takes the shape of the
 *  game's space on the Short as that changes (the webcam's share, the layout),
 *  and going back and forth doesn't wear it down, since the box drawn is kept
 *  and only this one is shown and used. */
export function reshape(box: Box, aspect: number, canvas: [number, number]): Box {
  const [W, H] = canvas
  const [x, y, w, h] = box
  if (Math.abs(w / h - aspect) <= 1e-9 * aspect) return box
  let nw = Math.sqrt(w * h * aspect)
  let nh = nw / aspect
  const fit = Math.min(1, W / nw, H / nh)
  nw *= fit
  nh *= fit
  return [clamp(x + w / 2 - nw / 2, 0, W - nw), clamp(y + h / 2 - nh / 2, 0, H - nh), nw, nh]
}
