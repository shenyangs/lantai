import { useEffect, useState, type ReactNode } from 'react'

export interface OfficeAgentEntryProps {
  readonly label: string
  readonly title: string
  readonly icon: ReactNode
  readonly active?: boolean | undefined
  readonly busy?: boolean | undefined
  readonly disabled?: boolean | undefined
  readonly onClick: () => void
}

/** The single Lantai co-creation entry shown in every local Office title row. */
export function OfficeAgentEntry({
  label,
  title,
  icon,
  active = false,
  busy = false,
  disabled = false,
  onClick,
}: OfficeAgentEntryProps): React.JSX.Element {
  const [elapsedSeconds, setElapsedSeconds] = useState(0)

  useEffect(() => {
    if (!busy) {
      setElapsedSeconds(0)
      return
    }
    const startedAt = Date.now()
    setElapsedSeconds(0)
    const timer = window.setInterval(
      () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000))),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [busy])

  return (
    <button
      type="button"
      className={`lt-office-agent-entry${active ? ' active' : ''}${busy ? ' busy' : ''}`}
      title={title}
      aria-label={title}
      aria-pressed={active}
      aria-busy={busy || undefined}
      disabled={disabled}
      onClick={onClick}
    >
      <span className="lt-office-agent-entry-mark" aria-hidden="true">
        {icon}
      </span>
      <span>{label}</span>
      {busy && (
        <span className="lt-office-agent-entry-progress" role="status" aria-live="polite">
          <span className="lt-office-agent-entry-pulse" aria-hidden="true" />
          {elapsedSeconds}s
        </span>
      )}
    </button>
  )
}
