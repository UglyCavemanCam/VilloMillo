import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText } from '../lib/api'
import { t } from '../lib/i18n'
import { workerSummary, type RemoteRenderState, type RenderWorker } from '../lib/remoteRender'

const DOCS = 'https://github.com/ColinGPT9/clips-studio/blob/main/docs/REMOTE-RENDERING.md'

/** Remote rendering: another PC of yours renders the clips, so this one
 *  stays free. Lives under Settings → Advanced settings, off by default:
 *  most people have one PC, and nothing about it shows until it is on. */
export default function RemoteRenderCard(): JSX.Element {
  const [state, setState] = useState<RemoteRenderState | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [pairing, setPairing] = useState<{ code: string; until: number; addresses: string[]; port: number;
    fingerprint: string } | null>(null)
  const [mainAddress, setMainAddress] = useState('')
  const [code, setCode] = useState('')

  const load = useCallback(async () => {
    try {
      setState(await api.remoteRender())
    } catch {
      // an engine without the feature: nothing to show
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // A render PC that just paired closes the code it used.
  const workerCount = state?.workers.length ?? 0
  const seen = useRef(workerCount)
  useEffect(() => {
    if (workerCount > seen.current) setPairing(null)
    seen.current = workerCount
  }, [workerCount])

  // While it's on, keep the workers' status current.
  const enabled = Boolean(state?.settings.enabled)
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(() => void load(), 3000)
    return () => clearInterval(id)
  }, [enabled, load])

  const run = async (fn: () => Promise<RemoteRenderState | unknown>): Promise<void> => {
    setBusy(true)
    setError('')
    try {
      const next = await fn()
      if (next && typeof next === 'object' && 'settings' in next) setState(next as RemoteRenderState)
      else await load()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  if (!state) return <p className="text-sm text-muted">{t('Loading…')}</p>

  const s = state.settings
  const tp = s.this_pc

  return (
    <div className="space-y-4">
      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          className="size-4 mt-0.5 accent-[#38BDF8]"
          checked={s.enabled}
          disabled={busy}
          onChange={(e) => void run(() => api.setRemoteRender({ enabled: e.target.checked }))}
        />
        <span>
          <span className="text-sm font-medium">{t('Remote rendering')}</span>
          <span className="block text-xs text-muted">
            {t('Another computer of yours renders the clips, so this one stays free while a long stream is processed. Both need Clips Kitty, on the same network or the same Tailscale tailnet.')}{' '}
            <button className="underline" onClick={() => void window.studio.openExternal(DOCS)}>
              {t('How it works')}
            </button>
          </span>
        </span>
      </label>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {s.enabled && (
        <>
          {state.gateway.error && (
            <p className="text-sm text-red-400">
              {t('The render gateway could not start:')} {state.gateway.error}
            </p>
          )}

          <section className="space-y-2">
            <h4 className="text-sm font-semibold">{t('Render clips on')}</h4>
            <RenderOn state={state} disabled={busy} onPick={(mode) => void run(() => api.setRemoteRender({ mode }))} />
          </section>

          <section className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-semibold">{t('Render PCs')}</h4>
              <button
                className="btn-ghost !py-1 !px-3 text-xs"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const p = await api.renderPairingCode()
                    setPairing({ ...p, until: Date.now() + p.expires_in * 1000 })
                    return null
                  })
                }
              >
                {t('Add a render PC')}
              </button>
            </div>
            {pairing && pairing.until > Date.now() && (
              <div className="rounded-lg bg-raised p-3 text-sm space-y-1">
                <p>
                  {t('On the other PC, open Settings → Advanced settings, turn on Remote rendering, and under "Use this PC as a render worker" enter:')}
                </p>
                <p>
                  {t('Main PC address')}:{' '}
                  <span className="font-mono text-ink">
                    {(pairing.addresses[0] ?? 'this-pc')}:{pairing.port}
                  </span>
                  {pairing.addresses.length > 1 && (
                    <span className="text-muted">
                      {' '}
                      ({t('or')} {pairing.addresses.slice(1).map((a) => `${a}:${pairing.port}`).join(', ')})
                    </span>
                  )}
                </p>
                <p>
                  {t('Pairing code')}: <span className="font-mono text-lg text-ink">{pairing.code}</span>{' '}
                  <span className="text-muted text-xs">{t('works once, for 10 minutes')}</span>
                </p>
                <p className="text-xs text-muted">
                  {t('Certificate')}: <span className="font-mono">{pairing.fingerprint}</span>{' '}
                  {t('(the other PC shows it too; they should match)')}
                </p>
              </div>
            )}
            {state.workers.length === 0 ? (
              <p className="text-sm text-muted">{t('No render PCs yet.')}</p>
            ) : (
              <ul className="space-y-2">
                {state.workers.map((w) => (
                  <WorkerRow
                    key={w.id}
                    w={w}
                    disabled={busy}
                    onHold={(held) => void run(() => api.holdRenderWorker(w.id, held))}
                    onRemove={() => void run(() => api.removeRenderWorker(w.id))}
                  />
                ))}
              </ul>
            )}
          </section>

          <section className="space-y-2 border-t border-line pt-3">
            <h4 className="text-sm font-semibold">{t('Use this PC as a render worker')}</h4>
            {!tp.paired ? (
              <>
                <p className="text-xs text-muted">
                  {t('Render clips for your main PC. Make a code there (Add a render PC), then enter its address and the code here.')}
                </p>
                <div className="flex flex-wrap gap-2">
                  <input
                    className="input !py-1 text-sm flex-1 min-w-44"
                    placeholder={t('Main PC address, e.g. 192.168.1.20:8766')}
                    value={mainAddress}
                    onChange={(e) => setMainAddress(e.target.value)}
                  />
                  <input
                    className="input !py-1 text-sm w-32 font-mono"
                    placeholder="ABCD-EFGH"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                  <button
                    className="btn-ghost !py-1 !px-3 text-xs"
                    disabled={busy || !mainAddress.trim() || !code.trim()}
                    onClick={() =>
                      void run(async () => {
                        const next = await api.setThisPcWorker({ main: mainAddress.trim(), code: code.trim(), enabled: true })
                        setCode('')
                        return next
                      })
                    }
                  >
                    {busy ? t('Pairing…') : t('Pair')}
                  </button>
                </div>
              </>
            ) : (
              <div className="space-y-2 text-sm">
                <p>
                  {t('Paired with')} <span className="font-mono">{tp.main}</span>{' '}
                  <span className="text-xs text-muted">
                    ({t('certificate')} {tp.fingerprint.toUpperCase().slice(0, 16).match(/.{2}/g)?.join(':')})
                  </span>
                </p>
                <p className="text-xs text-muted">
                  {!tp.enabled
                    ? t('Off: not rendering for the main PC.')
                    : state.this_pc.connected
                      ? t('Connected, waiting for clips to render.')
                      : state.this_pc.error || (state.this_pc.running ? t('Connecting…') : t('Not running.'))}
                </p>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    className="size-4 accent-[#38BDF8]"
                    checked={tp.enabled}
                    disabled={busy}
                    onChange={(e) => void run(() => api.setThisPcWorker({ enabled: e.target.checked }))}
                  />
                  {t('Render for the main PC')}
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    className="size-4 accent-[#38BDF8]"
                    checked={tp.draining}
                    disabled={busy || !tp.enabled}
                    onChange={(e) => void run(() => api.setThisPcWorker({ draining: e.target.checked }))}
                  />
                  {t('Stop accepting jobs (finish the current one)')}
                </label>
                <label className="flex items-center gap-2">
                  {t('Clips at a time')}
                  <select
                    className="input !py-0.5 !w-auto text-sm"
                    value={tp.max_jobs}
                    disabled={busy}
                    onChange={(e) => void run(() => api.setThisPcWorker({ max_jobs: Number(e.target.value) }))}
                  >
                    {[1, 2, 3, 4].map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                </label>
                <button className="btn-ghost !py-1 !px-3 text-xs" disabled={busy} onClick={() => void run(() => api.unpairThisPc())}>
                  {t('Unpair')}
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}

function RenderOn({
  state,
  disabled,
  onPick
}: {
  state: RemoteRenderState
  disabled: boolean
  onPick: (mode: string) => void
}): JSX.Element {
  const mode = state.settings.mode
  const options: [string, string, string][] = [
    ['local', t('This computer'), t('As always.')],
    ['auto', t('Automatic'), t('A render PC when one is free; this computer when none is.')],
    ...state.workers.map((w): [string, string, string] => [
      `worker:${w.id}`,
      `${t('Only on')} ${w.name}`,
      t('Clips wait for it if it is off, until you choose Render here instead.')
    ])
  ]
  return (
    <div className="space-y-1">
      {options.map(([value, label, note]) => (
        <label key={value} className="flex items-start gap-2 cursor-pointer text-sm">
          <input
            type="radio"
            name="render-on"
            className="mt-1 accent-[#38BDF8]"
            checked={mode === value}
            disabled={disabled}
            onChange={() => onPick(value)}
          />
          <span>
            {label} <span className="text-xs text-muted">{note}</span>
          </span>
        </label>
      ))}
    </div>
  )
}

function WorkerRow({
  w,
  disabled,
  onHold,
  onRemove
}: {
  w: RenderWorker
  disabled: boolean
  onHold: (held: boolean) => void
  onRemove: () => void
}): JSX.Element {
  const [confirm, setConfirm] = useState(false)
  const dot = { idle: 'bg-green-400', busy: 'bg-sky-400', draining: 'bg-amber-400', offline: 'bg-zinc-500' }[w.status]
  const status =
    w.status === 'busy' && w.current
      ? `${t('Rendering')} ${w.current.label} · ${w.current.stage}${
          ['transferring', 'uploading'].includes(w.current.stage) ? ` ${Math.round(w.current.progress * 100)}%` : ''
        }`
      : w.status === 'offline'
        ? `${t('Offline')}${w.last_seen ? ` · ${t('last seen')} ${new Date(w.last_seen * 1000).toLocaleString()}` : ''}`
        : w.status === 'draining'
          ? t('Not accepting jobs')
          : t('Idle')
  return (
    <li className="rounded-lg bg-raised p-3 text-sm">
      <div className="flex items-center gap-2">
        <span className={`size-2 rounded-full ${dot}`} aria-hidden />
        <span className="font-medium">{w.name}</span>
        <span className="text-xs text-muted truncate">{workerSummary(w)}</span>
      </div>
      <p className="text-xs text-muted mt-1">{status}</p>
      {w.caps.gpu?.vram_total_gb ? (
        <p className="text-xs text-muted">
          VRAM {w.caps.gpu.vram_used_gb ?? '?'} / {w.caps.gpu.vram_total_gb} GB
          {typeof w.caps.gpu.utilisation === 'number' ? ` · GPU ${w.caps.gpu.utilisation}%` : ''}
          {w.caps.framing === false ? ` · ${t('no face tracking: only whole-frame clips')}` : ''}
        </p>
      ) : null}
      <div className="flex gap-2 mt-2">
        <button className="btn-ghost !py-1 !px-3 text-xs" disabled={disabled} onClick={() => onHold(!w.held)}>
          {w.held ? t('Accept jobs again') : t('Stop accepting jobs')}
        </button>
        {confirm ? (
          <>
            <button className="btn-ghost !py-1 !px-3 text-xs" disabled={disabled} onClick={onRemove}>
              {t('Remove it')}
            </button>
            <button className="btn-ghost !py-1 !px-3 text-xs" onClick={() => setConfirm(false)}>
              {t('Cancel')}
            </button>
          </>
        ) : (
          <button className="btn-ghost !py-1 !px-3 text-xs" disabled={disabled} onClick={() => setConfirm(true)}>
            {t('Remove')}
          </button>
        )}
      </div>
    </li>
  )
}
