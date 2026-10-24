export interface OfficeAgentRunReceiptData {
  readonly status: 'completed' | 'partial' | 'cancelled' | 'failed'
  readonly toolCount: number
  readonly completedCount: number
  readonly failedCount: number
  readonly mutationCount: number
}

export interface OfficeAgentRunReceiptProps {
  readonly receipt: OfficeAgentRunReceiptData
  readonly rollbackAvailable?: boolean | undefined
}

const STATUS_LABEL: Record<OfficeAgentRunReceiptData['status'], string> = {
  completed: '本轮完成',
  partial: '部分完成',
  cancelled: '已停止',
  failed: '执行失败',
}

/** Shared, factual run receipt. It reports execution—not an unverified save or delivery claim. */
export function OfficeAgentRunReceipt({
  receipt,
  rollbackAvailable = false,
}: OfficeAgentRunReceiptProps): React.JSX.Element {
  const tone = receipt.failedCount > 0 || receipt.status === 'failed' ? 'warning' : receipt.status
  return (
    <div
      className={`lt-office-agent-receipt ${tone}`}
      role="status"
      data-office-agent-receipt={receipt.status}
    >
      <span className="lt-office-agent-receipt-dot" aria-hidden="true" />
      <span className="lt-office-agent-receipt-title">{STATUS_LABEL[receipt.status]}</span>
      <span className="lt-office-agent-receipt-detail">
        {receipt.toolCount} 步
        {receipt.mutationCount > 0 ? ` · ${receipt.mutationCount} 项改动` : ' · 只读'}
        {receipt.failedCount > 0 ? ` · ${receipt.failedCount} 项未通过` : ''}
        {rollbackAvailable && receipt.mutationCount > 0 ? ' · 可撤销' : ''}
      </span>
    </div>
  )
}
