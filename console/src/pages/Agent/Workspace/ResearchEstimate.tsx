import { useEffect, useState } from "react";
import { Alert, Typography } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type ResearchEstimateResult, type ResearchObservedRange, type ResearchProject } from "@/api/modules/research";
import styles from "./research.module.less";

const number = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
function range(value: ResearchObservedRange, samples: number, unit: string) {
  const span = value.min === null || value.max === null ? "未知" : `${number(value.min)}–${number(value.max)} ${unit}`;
  return `${span}（${value.known_samples}/${samples} 次有记录）`;
}

export default function ResearchEstimate({ project }: { project: ResearchProject }) {
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{ owner: ResearchProject; refresh: number; value?: ResearchEstimateResult; failed?: boolean }>();
  useEffect(() => {
    let active = true;
    setState({ owner: project, refresh });
    void researchApi.estimate(project.project_id, project.revision).then((value) => {
      if (!active) return;
      const b = value.binding;
      if (b.project_id !== project.project_id || b.revision !== project.revision || b.report_id !== project.report_id ||
        JSON.stringify(b.source_versions) !== JSON.stringify(project.source_versions)) {
        setState({ owner: project, refresh, failed: true });
      } else setState({ owner: project, refresh, value });
    }).catch(() => { if (active) setState({ owner: project, refresh, failed: true }); });
    return () => { active = false; };
  }, [project, refresh]);
  const current = state?.owner === project && state.refresh === refresh ? state : undefined;
  const value = current?.value;
  return <section aria-label="运行前估算">
    <Typography.Title level={5}>运行前估算</Typography.Title>
    <Button onClick={() => setRefresh((n) => n + 1)}>刷新估算</Button>
    {!current?.failed && !value && <p role="status">正在读取本地记录…</p>}
    {current?.failed && <Alert type="warning" showIcon message="估算暂不可用，请刷新项目后重试。当前没有可用的估算数字。" />}
    {value && <>
      <p className={styles.muted}>修订 {value.binding.revision} · 截至 {new Date(value.generated_at * 1000).toLocaleTimeString("zh-CN")}；继续研究前可刷新。本次查看不调用模型。</p>
      {value.reading.status === "unknown" ? <p>尚未导入资料，阅读量未知。</p> : <>
        <p>当前已索引资料尚有 {number(value.reading.remaining_blocks)} 个未读块，共 {number(value.reading.remaining_chars)} 字符。</p>
        <p>读完这些块需新增阅读额度 {number(value.reading.additional_budget_chars)} 字符；当前剩余额度 {number(value.reading.budget_remaining_chars)} 字符。</p>
        {value.reading.status === "partial" && <Alert type="warning" showIcon message="资料未完整解析或已变化，上述数字只覆盖当前索引，不含缺失内容及后续新增资料。" />}
        {!value.reading.fits_budget && <Alert type="warning" showIcon message="阅读预算不足以读完当前未读块，可在下方调整预算。" />}
      </>}
      <p>完整流程的模型调用次数、Token、耗时和费用：未知。尚无适用的完整流程校准数据；预算上限不代表预计用量或费用。</p>
      {!!value.exhausted_budgets.length && <p>部分预算已耗尽，继续前请查看下方预算与用量。</p>}
      {!!value.historical_calls.length && <details>
        <summary>查看单次调用历史范围（{value.historical_calls.length} 组）</summary>
        <p>仅统计当前修订内已完成调用，按模型配置及用途分组；不代表下一次或完整研究的预测区间。</p>
        {value.historical_calls.map((row) => <div key={`${row.config_hash}:${row.surface}`}>
          <p>{row.provider} / {row.model} · {row.surface} · 配置 {row.config_hash.slice(0, 12)} · {row.samples} 次已完成调用</p>
          <p>Token：{range(row.tokens, row.samples, "Token")}；耗时：{range(row.seconds, row.samples, "秒")}；费用：{range(row.cost_cny, row.samples, "元")}</p>
        </div>)}
      </details>}
      {!!value.excluded_incomplete_calls && <p className={styles.muted}>{value.excluded_incomplete_calls} 次未完成调用未计入历史范围，其实际远端用量可能未知。</p>}
    </>}
  </section>;
}
