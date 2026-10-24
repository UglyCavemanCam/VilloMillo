import { t } from '../lib/i18n'
import type { ClipDirection } from '../lib/types'

/** What the clip direction given with this video was understood as, and what
 *  came of it (analysis/intent.py).
 *
 *  Above the clips, because a "make sure you include X" that was never said in
 *  the video should say so where the person is looking: no clip was made for
 *  it, and nothing was invented to stand in for it. */
export default function ClipDirectionNote({ direction }: { direction: ClipDirection }): JSX.Element {
  const { understood, not_found, not_applied, notes, boosted, added_windows } = direction
  return (
    <div className="border border-raised/60 rounded-lg p-3 space-y-1.5 text-sm">
      <p>
        <span className="font-medium text-ink">{t('Clip direction')}</span>{' '}
        <span className="text-muted">“{direction.direction}”</span>
      </p>
      {understood.length > 0 && (
        <p className="text-xs text-muted">
          {t('Understood:')} <span className="text-ink">{understood.join(' · ')}</span>
        </p>
      )}
      {not_found.map((what) => (
        <p key={what} className="text-xs text-amber-300">
          {t("Couldn't find:")} {what}. {t('No clip was made for it.')}
        </p>
      ))}
      {not_applied.length > 0 && (
        <p className="text-xs text-muted">
          {t('Not applied (a direction only adds weight):')} {not_applied.join(' · ')}
        </p>
      )}
      {notes.map((note) => (
        <p key={note} className="text-xs text-muted">
          {note}
        </p>
      ))}
      <p className="text-xs text-muted">
        {boosted} {t('moments got extra points')}
        {added_windows > 0 ? ` · ${added_windows} ${t('more moments looked at for it')}` : ''}
      </p>
    </div>
  )
}
