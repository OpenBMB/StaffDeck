import { IconCheckOutline16, IconWarningOutline16 } from './icons/index.tsx'
import css from './ConnectionIndicator.module.css'

/** Visual state rendered by {@link ConnectionIndicator}. */
export type ConnectionIndicatorState =
  | 'disconnected'
  | 'connecting'
  | 'recovered'

/**
 * Render an inline connection-recovery control.
 * @param props.state - visible outage, retry-attempt, or recovered state.
 * @param props.disconnectedLabel - localized outage text.
 * @param props.reconnectLabel - localized action text shown on hover or focus.
 * @param props.connectingLabel - localized retry text followed by the attempt dots.
 * @param props.recoveredLabel - localized recovery confirmation.
 * @param props.reconnectActionLabel - accessible label for the outage action.
 * @param props.restartActionLabel - accessible label for replacing an active attempt.
 * @param props.onReconnect - request an immediate reconnect attempt.
 * @returns the indicator, or null when no connection feedback is active.
 */
export function ConnectionIndicator({
  state,
  disconnectedLabel,
  reconnectLabel,
  connectingLabel,
  recoveredLabel,
  reconnectActionLabel,
  restartActionLabel,
  onReconnect,
}: {
  state: ConnectionIndicatorState | undefined
  disconnectedLabel: string
  reconnectLabel: string
  connectingLabel: string
  recoveredLabel: string
  reconnectActionLabel: string
  restartActionLabel: string
  onReconnect: () => void
}) {
  if (state === undefined) return null
  const sizeLabels = (
    <>
      <span className={css.sizeLabel} aria-hidden="true">{disconnectedLabel}</span>
      <span className={css.sizeLabel} aria-hidden="true">{reconnectLabel}</span>
      <span className={css.sizeLabel} aria-hidden="true">
        {connectingLabel}<span className={css.dots}>...</span>
      </span>
      <span className={css.sizeLabel} aria-hidden="true">{recoveredLabel}</span>
    </>
  )
  if (state === 'recovered') {
    return (
      <div className={`${css.indicator} ${css.success}`} role="status" aria-label={recoveredLabel}>
        <span className={css.icon} aria-hidden="true"><IconCheckOutline16 size={14} /></span>
        <span className={css.label}>
          {sizeLabels}
          <span className={css.stateLabel}>{recoveredLabel}</span>
        </span>
      </div>
    )
  }

  const connecting = state === 'connecting'
  return (
    <button
      type="button"
      className={`${css.indicator} ${css.warning}`}
      data-phase={state}
      aria-label={connecting ? restartActionLabel : reconnectActionLabel}
      onClick={onReconnect}
    >
      <span className={css.icon} aria-hidden="true"><IconWarningOutline16 size={14} /></span>
      <span className={css.label}>
        {sizeLabels}
        <span className={css.stateLabel}>
          {connecting
            ? (
              <>
                {connectingLabel}
                <span className={css.dots} aria-hidden="true">
                  <span>.</span>
                  <span className={css.secondDot}>.</span>
                  <span className={css.thirdDot}>.</span>
                </span>
              </>
            )
            : disconnectedLabel}
        </span>
        <span className={css.hoverLabel}>{reconnectLabel}</span>
      </span>
    </button>
  )
}
