/** Remote rendering (remote_render/ in the engine): a second PC renders
 *  clips for this one. Off by default; only Settings → Advanced settings
 *  shows it, and only once it is switched on does anything else appear. */

export interface RenderWorker {
  id: string
  name: string
  status: 'idle' | 'busy' | 'draining' | 'offline'
  online: boolean
  last_seen: number
  draining: boolean
  held: boolean
  max_jobs: number
  running: number
  current: { label: string; stage: string; progress: number } | null
  caps: {
    version?: string
    os?: string
    cpu?: string
    cores?: number
    ram_gb?: number
    gpu?: { name?: string; vram_total_gb?: number; vram_used_gb?: number; utilisation?: number }
    encoders?: string[]
    ffmpeg?: string
    framing?: boolean
  }
}

export interface RemoteRenderState {
  settings: {
    enabled: boolean
    mode: string
    port: number
    this_pc: {
      enabled: boolean
      main: string
      fingerprint: string
      worker_id: string
      max_jobs: number
      draining: boolean
      paired: boolean
    }
  }
  gateway: { running: boolean; port: number; addresses: string[]; fingerprint: string; error: string }
  workers: RenderWorker[]
  this_pc: {
    running: boolean
    connected?: boolean
    error?: string
    main?: string
    draining?: boolean
  }
}

/** "NVIDIA GeForce RTX 3060 · NVENC" for a worker's one-line summary. */
export function workerSummary(w: RenderWorker): string {
  const gpu = w.caps.gpu?.name
  const enc = (w.caps.encoders ?? []).filter((e) => e !== 'cpu')
  const encoder = enc.length ? enc[0].replace(/^(h264_)?/, '').toUpperCase() : 'CPU encoding'
  return [gpu, encoder, w.caps.version ? `v${w.caps.version}` : ''].filter(Boolean).join(' · ')
}
