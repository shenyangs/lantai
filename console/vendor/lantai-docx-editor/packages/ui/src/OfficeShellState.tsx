import type { ReactNode } from 'react'

export interface OfficeShellStateProps {
  readonly title: string
  readonly description?: string | undefined
  readonly tone?: 'loading' | 'empty' | 'error' | undefined
  readonly action?: ReactNode
}

/** Shared full-window state for local Office surfaces before their editing canvas is ready. */
export function OfficeShellState({
  title,
  description,
  tone = 'empty',
  action,
}: OfficeShellStateProps): React.JSX.Element {
  const isLoading = tone === 'loading'
  const isError = tone === 'error'

  return (
    <section
      className={`lt-office-state lt-office-state-${tone}`}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
      aria-busy={isLoading || undefined}
    >
      <span className="lt-office-state-mark" aria-hidden="true" />
      <h1>{title}</h1>
      {description && <p>{description}</p>}
      {action && <div className="lt-office-state-action">{action}</div>}
    </section>
  )
}
