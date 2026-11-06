import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import css from './OnboardingSurface.module.css'

/**
 * Render a body-portaled onboarding stage and keep the application root inert
 * while mounted.
 * @param props.children - the step's page content, centered on the stage.
 * @returns the body-portaled overlay tree.
 */
export function OnboardingSurface({ children }: { children: ReactNode }) {
  useEffect(() => {
    const appRoot = document.getElementById('root')
    if (appRoot === null) return
    appRoot.inert = true
    return () => { appRoot.inert = false }
  }, [])

  return createPortal((
    <div className={css.onboardingOverlay} role="presentation">
      <div className={css.onboardingMask} aria-hidden="true" />
      <div className={css.onboardingStage}>{children}</div>
    </div>
  ), document.body)
}
