export interface OfficeAgentCapability {
  readonly id: string
  readonly label: string
  readonly title?: string | undefined
  /** False means the runtime has no executable path; the action is not rendered. */
  readonly available?: boolean | undefined
  /** Short runtime proof label for diagnostics and UI contract tests. */
  readonly evidence?: string | undefined
  readonly disabled?: boolean | undefined
  readonly onInvoke: () => void
}

export interface OfficeAgentCapabilitiesProps {
  readonly actions: readonly OfficeAgentCapability[]
}

/**
 * Compact, executable shortcuts hosted inside the co-creation panel.
 * Callers own the real Agent/tool invocation; this component never advertises
 * an action without an attached handler.
 */
export function OfficeAgentCapabilities({
  actions,
}: OfficeAgentCapabilitiesProps): React.JSX.Element | null {
  const availableActions = actions.filter((action) => action.available !== false)
  if (availableActions.length === 0) return null

  return (
    <div className="lt-office-agent-capabilities">
      {availableActions.map((action) => (
        <button
          key={action.id}
          type="button"
          className="lt-office-agent-capability"
          data-office-agent-capability={action.id}
          data-office-agent-evidence={action.evidence}
          title={action.title ?? action.label}
          disabled={action.disabled}
          onClick={action.onInvoke}
        >
          <span>{action.label}</span>
          <span className="lt-office-agent-capability-arrow" aria-hidden="true">
            ↗
          </span>
        </button>
      ))}
    </div>
  )
}
