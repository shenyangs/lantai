import { useEffect, useState } from 'react'
import type React from 'react'

/**
 * The desktop host pins a concrete model to each Office editor window when it
 * opens.  Keep that fact visible at the point of collaboration so an editor
 * never silently follows a later global/chat model change.
 *
 * This component intentionally accepts the vendor's extensible settings shape
 * instead of importing an app-specific IPC type: Docs, Sheets, Slides and PDF
 * each compile against a different generated settings package.
 */
export interface OfficeAgentModelBindingProps {
  readonly settings?: unknown
  /** Changes only this editor window; it must never mutate global/chat defaults. */
  readonly onSelect?: ((binding: OfficeAgentModelWindowBinding) => Promise<void> | void) | undefined
}

export interface OfficeAgentModelWindowBinding {
  readonly provider_id: string
  readonly model: string
  readonly source?: string
  readonly label?: string
}

type StringMap = Record<string, unknown>

function asRecord(value: unknown): StringMap | null {
  return value !== null && typeof value === 'object' ? (value as StringMap) : null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function resolveModel(settings: unknown): {
  readonly model: string
  readonly provider: string | null
  readonly source: string | null
} {
  const config = asRecord(settings)
  const binding = asRecord(config?.lantaiModelBinding)
  const boundModel = text(binding?.model)
  if (boundModel) {
    return {
      model: boundModel,
      provider: text(binding?.provider_id),
      source: text(binding?.source),
    }
  }

  const provider = text(config?.provider)
  const providers = asRecord(config?.providers)
  const providerConfig = provider ? asRecord(providers?.[provider]) : null
  return {
    model: text(providerConfig?.model) ?? '正在确认',
    provider,
    source: null,
  }
}

function resolveOptions(settings: unknown): OfficeAgentModelWindowBinding[] {
  const config = asRecord(settings)
  const values = Array.isArray(config?.lantaiModelOptions) ? config.lantaiModelOptions : []
  const unique = new Map<string, OfficeAgentModelWindowBinding>()
  for (const value of values) {
    const option = asRecord(value)
    const providerId = text(option?.provider_id)
    const model = text(option?.model)
    if (!providerId || !model) continue
    unique.set(`${providerId}:${model}`, {
      provider_id: providerId,
      model,
      source: 'window_selection',
      label: text(option?.label) ?? model,
    })
  }
  return [...unique.values()]
}

function optionKey(binding: Pick<OfficeAgentModelWindowBinding, 'provider_id' | 'model'>): string {
  return `${binding.provider_id}:${binding.model}`
}

function sourceLabel(source: string | null): string | null {
  if (source === 'chat_snapshot') return '会话快照'
  if (source === 'default_snapshot') return '默认快照'
  if (source === 'window_selection') return '本窗口选择'
  return source ? '窗口绑定' : null
}

export function OfficeAgentModelBinding({ settings, onSelect }: OfficeAgentModelBindingProps): React.JSX.Element {
  const resolved = resolveModel(settings)
  const options = resolveOptions(settings)
  const [binding, setBinding] = useState(resolved)
  const [switching, setSwitching] = useState(false)
  const [switchError, setSwitchError] = useState<string | null>(null)

  useEffect(() => {
    setBinding(resolved)
  }, [resolved.model, resolved.provider, resolved.source])

  const details = [binding.provider, binding.source ? `窗口${binding.source}` : null]
    .filter((value): value is string => Boolean(value))
    .join(' · ')

  // A pinned model can have disappeared from the current configured option
  // list. Keep it selectable and visible until the user deliberately changes
  // it instead of rendering a select whose value has no matching option.
  const currentOption = binding.provider
    ? {
        provider_id: binding.provider,
        model: binding.model,
        label: binding.model,
        ...(binding.source ? { source: binding.source } : {}),
      }
    : null
  const selectableOptions = currentOption && !options.some((option) => optionKey(option) === optionKey(currentOption))
    ? [currentOption, ...options]
    : options
  const canSelect = Boolean(onSelect) && selectableOptions.length > 1
  return (
    <span
      className="lt-office-agent-model-binding"
      data-office-model-binding={binding.model}
      title={details ? `当前窗口固定使用 ${binding.model}（${details}）` : `当前窗口固定使用 ${binding.model}`}
    >
      {canSelect ? (
        <label>
          <span className="sr-only">切换当前编辑窗口模型</span>
          <select
            aria-label="切换当前编辑窗口模型"
            disabled={switching}
            value={`${binding.provider ?? ''}:${binding.model}`}
            onChange={(event) => {
              const selected = selectableOptions.find(
                (option) => optionKey(option) === event.target.value,
              )
              if (!selected || !onSelect) return
              const previous = binding
              setBinding({ model: selected.model, provider: selected.provider_id, source: 'window_selection' })
              setSwitching(true)
              setSwitchError(null)
              void Promise.resolve(onSelect(selected))
                .catch((error: unknown) => {
                  setBinding(previous)
                  setSwitchError(error instanceof Error ? error.message : '模型切换失败')
                })
                .finally(() => setSwitching(false))
            }}
          >
            {selectableOptions.map((option) => (
              <option key={optionKey(option)} value={optionKey(option)}>
                {option.label ?? option.model}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <>当前模型：{binding.model}</>
      )}
      {sourceLabel(binding.source) && <em>· {sourceLabel(binding.source)}</em>}
      {switchError && <span className="sr-only" role="alert">{switchError}</span>}
    </span>
  )
}
