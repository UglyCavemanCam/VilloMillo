import { t } from '../lib/i18n'
import type { SportReport } from '../lib/types'

/** What a match gave (sports/core/clips.py report): the moments found by
 *  type, the score read off the scoreboard, and what couldn't be confirmed.
 *
 *  Above the clips, like the clip direction's note, so a match that gave
 *  fewer goals than it had says why where the person is looking. */
export default function SportsNote({ report }: { report: SportReport }): JSX.Element {
  const found = Object.entries(report.found)
  const extra = [
    report.big_moments > 0 ? `${report.big_moments} ${t('big moments')}` : '',
    report.replays_grouped > 0 ? `${report.replays_grouped} ${t('replays kept with their moment')}` : ''
  ].filter(Boolean)
  return (
    <div className="border border-raised/60 rounded-lg p-3 space-y-1.5 text-sm">
      <p>
        <span className="font-medium text-ink">{report.sport}</span>
        {report.footage === 'sideline' && <span className="text-muted"> · {t('club or phone footage')}</span>}
        {report.score && (
          <span className="text-muted">
            {' '}
            · {t('score read')} {report.score}
          </span>
        )}
      </p>
      <p className="text-xs text-muted">
        {found.length > 0 ? (
          <>
            {t('Found:')}{' '}
            <span className="text-ink">{found.map(([label, n]) => `${label} ×${n}`).join(' · ')}</span>
          </>
        ) : (
          t('No moment could be named for sure.')
        )}
        {extra.length > 0 ? ` · ${extra.join(' · ')}` : ''}
      </p>
      {!report.scoreboard && report.footage !== 'sideline' && (
        <p className="text-xs text-amber-300">
          {t('No scoreboard was found on screen, so goals were found from the crowd and the commentary alone.')}
        </p>
      )}
      {report.notes.map((note) => (
        <p key={note} className="text-xs text-muted">
          {note}
        </p>
      ))}
    </div>
  )
}
