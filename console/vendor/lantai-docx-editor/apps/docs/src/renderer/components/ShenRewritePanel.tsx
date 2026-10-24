import { useState } from 'react'
import { activeShenRewriteVersion, type ShenRewriteProposalState } from '../shen-rewrite-proposals'

function excerpt(value: string, max = 1600): string {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

export function ShenRewritePanel({
  proposals,
  busy,
  onAccept,
  onReject,
  onDraftChange,
  onRefine,
  onRetry,
  onSelectVersion,
}: {
  proposals: ShenRewriteProposalState[]
  busy: boolean
  onAccept: (proposalId: string) => void
  onReject: (proposalId: string) => void
  onDraftChange: (proposalId: string, value: string) => void
  onRefine: (proposalId: string, instruction: string) => void
  onRetry: (proposalId: string) => void
  onSelectVersion: (proposalId: string, versionIndex: number) => void
}) {
  const [instructions, setInstructions] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  return (
    <aside
      className="shen-rewrite-panel is-ready"
      data-co-creation-surface="office-docs-rewrite"
      aria-live="polite"
    >
      <header className="shen-rewrite-panel-header">
        <span className="shen-rewrite-panel-spark" aria-hidden="true">
          ✦
        </span>
        <div>
          <strong>沈行知的改写建议</strong>
          <p>已保留 {proposals.length} 条建议；可以继续选文改写，接受前不会写入原文。</p>
        </div>
      </header>
      <div className="shen-rewrite-panel-body">
        {proposals.map((proposal, index) => {
          const version = activeShenRewriteVersion(proposal)
          const ready = proposal.phase === 'ready'
          const running = proposal.phase === 'running'
          const instruction = instructions[proposal.id] ?? ''
          return (
            <details
              className={`shen-rewrite-card is-${proposal.phase}`}
              key={proposal.id}
              open={expanded[proposal.id] ?? index === proposals.length - 1}
              onToggle={(event) => {
                const open = event.currentTarget.open
                setExpanded((current) =>
                  current[proposal.id] === open ? current : { ...current, [proposal.id]: open },
                )
              }}
            >
              <summary>
                <span>建议 {index + 1}</span>
                <b>
                  {running
                    ? '改写中'
                    : proposal.phase === 'conflict'
                      ? '原文有冲突'
                      : proposal.phase === 'failed'
                        ? '随改失败'
                        : `版本 ${proposal.versionIndex + 1}/${proposal.versions.length}`}
                </b>
              </summary>
              <div className="shen-rewrite-card-content">
                {running && (
                  <div className="shen-rewrite-panel-loading" role="status">
                    <span />
                    <span />
                    <span />
                    <b>正在阅读上下文并组织表达…</b>
                  </div>
                )}
                {proposal.error && <p className="shen-rewrite-conflict">{proposal.error}</p>}
                {(proposal.phase === 'failed' || (ready && proposal.error)) && (
                  <button
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => onRetry(proposal.id)}
                  >
                    {ready ? '重试继续修改' : '重试生成'}
                  </button>
                )}
                {version?.summary && <p className="shen-rewrite-summary">{version.summary}</p>}
                <section>
                  <h3>原文</h3>
                  <div className="shen-rewrite-original">
                    {excerpt(proposal.snapshot.selectedText)}
                  </div>
                </section>
                {version && (
                  <section>
                    <div className="shen-rewrite-section-title">
                      <h3>建议改为（可以直接编辑）</h3>
                      {proposal.versions.length > 1 && (
                        <div className="shen-rewrite-version-nav">
                          <button
                            disabled={proposal.versionIndex === 0 || busy}
                            onClick={() => onSelectVersion(proposal.id, proposal.versionIndex - 1)}
                          >
                            ‹
                          </button>
                          <span>
                            {proposal.versionIndex + 1}/{proposal.versions.length}
                          </span>
                          <button
                            disabled={
                              proposal.versionIndex === proposal.versions.length - 1 || busy
                            }
                            onClick={() => onSelectVersion(proposal.id, proposal.versionIndex + 1)}
                          >
                            ›
                          </button>
                        </div>
                      )}
                    </div>
                    <textarea
                      className="shen-rewrite-replacement"
                      value={version.replacementText}
                      disabled={!ready || busy}
                      onChange={(event) => onDraftChange(proposal.id, event.target.value)}
                    />
                  </section>
                )}
                {ready && (
                  <section className="shen-rewrite-refine">
                    <h3>让沈行知继续改</h3>
                    <textarea
                      value={instruction}
                      disabled={busy}
                      maxLength={2000}
                      placeholder="例如：再短一点，保留第二条数据"
                      onChange={(event) =>
                        setInstructions((current) => ({
                          ...current,
                          [proposal.id]: event.target.value,
                        }))
                      }
                    />
                    <button
                      className="btn-ghost"
                      disabled={busy || !instruction.trim()}
                      onClick={() => {
                        onRefine(proposal.id, instruction.trim())
                        setInstructions((current) => ({
                          ...current,
                          [proposal.id]: '',
                        }))
                      }}
                    >
                      继续改这一版
                    </button>
                  </section>
                )}
                <footer className="shen-rewrite-card-actions">
                  <button
                    className="btn-ghost"
                    disabled={busy && !running}
                    onClick={() => onReject(proposal.id)}
                  >
                    {running ? '取消' : '拒绝'}
                  </button>
                  <button
                    className="btn-primary"
                    disabled={!ready || busy || !version?.replacementText.trim()}
                    onClick={() => onAccept(proposal.id)}
                  >
                    接受并替换选区
                  </button>
                </footer>
              </div>
            </details>
          )
        })}
      </div>
    </aside>
  )
}
