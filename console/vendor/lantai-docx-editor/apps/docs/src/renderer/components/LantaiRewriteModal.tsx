import { useEffect, useRef, useState } from 'react'

export function ShenRewriteModal({
  selectedText,
  onSubmit,
  onClose,
}: {
  selectedText: string
  onSubmit: (instruction: string) => void
  onClose: () => void
}) {
  const [instruction, setInstruction] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => inputRef.current?.focus(), [])

  const submit = () => {
    const value = instruction.trim()
    if (!value) return
    onSubmit(value)
    onClose()
  }

  const preview = selectedText.length > 280 ? `${selectedText.slice(0, 280)}…` : selectedText
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal lantai-rewrite-modal" role="dialog" aria-modal="true" aria-labelledby="lantai-rewrite-title">
        <h2 id="lantai-rewrite-title">交给沈行知</h2>
        <p className="lantai-rewrite-intro">说说你想怎么修改这段文字。沈行知会先在右侧给出建议，接受后才替换选区。</p>
        <div className="lantai-rewrite-selection" aria-label="已选文字">{preview}</div>
        <label>
          修改要求
          <textarea
            ref={inputRef}
            className="prompt-textarea lantai-rewrite-input"
            rows={4}
            maxLength={2000}
            value={instruction}
            placeholder="例如：写得更简洁，保留原意和数据"
            onChange={(event) => setInstruction(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) submit()
            }}
          />
        </label>
        <p className="lantai-rewrite-privacy">为保持事实、逻辑和风格一致，将发送选中文字、当前文档正文及来源会话给当前使用的模型；不会读取长期记忆。</p>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onClose}>取消</button>
          <button className="btn-primary" disabled={!instruction.trim()} onClick={submit}>交给沈行知</button>
        </div>
      </div>
    </div>
  )
}
