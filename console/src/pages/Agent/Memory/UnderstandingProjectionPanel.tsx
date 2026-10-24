import { useMemo } from "react";
import { Button, Empty } from "antd";
import { ArrowLeft, Check, FolderKanban, X } from "lucide-react";
import type { MemoryFact, UserUnderstandingAtom } from "../../../api/types";
import { FactSources } from "./FactSources";
import styles from "./UnderstandingProjectionPanel.module.less";

interface Props {
  agentId: string;
  facts: MemoryFact[];
  hypotheses: UserUnderstandingAtom[];
  deferredIds: Set<string>;
  onBack: () => void;
  onDefer: (id: string) => void;
  onResetDeferred: () => void;
  onFactConfirm: (fact: MemoryFact, scope: "global" | "project") => void;
  onFactReject: (fact: MemoryFact) => void;
  onSourceDeleted?: () => void;
  onHypothesisFeedback: (
    atomId: string,
    verdict: "confirmed" | "corrected",
  ) => void;
}

type ReviewItem =
  | { kind: "fact"; id: string; item: MemoryFact }
  | { kind: "hypothesis"; id: string; item: UserUnderstandingAtom };

function scopeHint(fact: MemoryFact) {
  if (fact.sensitivity !== "normal") {
    return "这条信息比较私人，只有你决定保存后才会继续使用。";
  }
  if (fact.scope === "project" || fact.subject === "project") {
    return "请决定它只属于当前项目，还是以后在其他对话也适用。";
  }
  return "澜台不确定这是否值得长期使用，你可以确认、限制范围或拒绝。";
}

export function UnderstandingProjectionPanel({
  agentId,
  facts,
  hypotheses,
  deferredIds,
  onBack,
  onDefer,
  onResetDeferred,
  onFactConfirm,
  onFactReject,
  onSourceDeleted,
  onHypothesisFeedback,
}: Props) {
  const items = useMemo<ReviewItem[]>(
    () => [
      ...facts.map((item) => ({
        kind: "fact" as const,
        id: `fact:${item.id}`,
        item,
      })),
      ...hypotheses.map((item) => ({
        kind: "hypothesis" as const,
        id: `hypothesis:${item.id}`,
        item,
      })),
    ],
    [facts, hypotheses],
  );
  const visible = items.filter((item) => !deferredIds.has(item.id));

  return (
    <section className={styles.reviewSurface} aria-label="需要核对">
      <header className={styles.reviewHeader}>
        <Button type="text" icon={<ArrowLeft size={16} />} onClick={onBack}>
          返回记忆
        </Button>
        <div>
          <h2>需要核对</h2>
          <p>这些选择彼此独立。可以只处理一项，也可以全部留到以后。</p>
        </div>
      </header>

      {visible.length ? (
        <div className={styles.reviewList}>
          {visible.map((review) => {
            const statement =
              review.kind === "fact"
                ? review.item.content
                : review.item.statement;
            return (
              <article key={review.id} className={styles.reviewItem}>
                <div className={styles.reviewCopy}>
                  <h3>
                    {review.kind === "fact"
                      ? "这条信息以后可以继续使用吗？"
                      : "澜台这样理解你，对吗？"}
                  </h3>
                  <p className={styles.statement}>{statement}</p>
                  <p className={styles.consequence}>
                    {review.kind === "fact"
                      ? scopeHint(review.item)
                      : "确认后会影响以后相关对话；否认后，澜台会停止沿用这项理解。"}
                  </p>
                  {review.kind === "fact" ? (
                    <FactSources factId={review.item.id} agentId={agentId} onChanged={onSourceDeleted} />
                  ) : null}
                </div>
                <div className={styles.reviewActions}>
                  {review.kind === "fact" ? (
                    <>
                      <Button
                        type="primary"
                        icon={<Check size={15} />}
                        onClick={() => onFactConfirm(review.item, "global")}
                      >
                        以后都可以用
                      </Button>
                      <Button
                        icon={<FolderKanban size={15} />}
                        onClick={() => onFactConfirm(review.item, "project")}
                      >
                        只在这个项目用
                      </Button>
                      <Button
                        type="text"
                        icon={<X size={15} />}
                        onClick={() => onFactReject(review.item)}
                      >
                        不要记住
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        type="primary"
                        icon={<Check size={15} />}
                        onClick={() =>
                          onHypothesisFeedback(review.item.id, "confirmed")
                        }
                      >
                        是这样
                      </Button>
                      <Button
                        icon={<X size={15} />}
                        onClick={() =>
                          onHypothesisFeedback(review.item.id, "corrected")
                        }
                      >
                        不是这样
                      </Button>
                    </>
                  )}
                  <Button type="text" onClick={() => onDefer(review.id)}>
                    以后再说
                  </Button>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className={styles.reviewEmpty}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              deferredIds.size
                ? "这次先不处理，其他记忆和聊天不受影响。"
                : "目前没有需要你核对的内容。"
            }
          />
          {deferredIds.size ? (
            <Button type="text" onClick={onResetDeferred}>
              重新查看
            </Button>
          ) : null}
        </div>
      )}
    </section>
  );
}
