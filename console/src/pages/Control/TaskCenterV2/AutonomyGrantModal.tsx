import {
  Alert,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
} from "antd";
import { useEffect, useMemo, useState } from "react";

import type {
  RuntimeAutoResumeCondition,
  RuntimeAutonomyGrantRequest,
  RuntimeAutonomyOperation,
  RuntimeTaskBoardItem,
} from "../../../api/types/runtime";
import styles from "./index.module.less";

interface AutonomyGrantModalProps {
  item: RuntimeTaskBoardItem | null;
  open: boolean;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (
    item: RuntimeTaskBoardItem,
    request: RuntimeAutonomyGrantRequest,
  ) => Promise<boolean>;
}

const LOCAL_OPERATIONS: RuntimeAutonomyOperation[] = [
  "local_read",
  "local_write_new",
  "local_modify_existing",
];

const DEFAULT_OPERATIONS: RuntimeAutonomyOperation[] = [
  "automatic_resume",
  "model_call",
  ...LOCAL_OPERATIONS,
];

const DEFAULT_RESUME_CONDITIONS: RuntimeAutoResumeCondition[] = [
  "backend_restart",
  "app_reopen",
  "network_recovery",
];

const OPERATION_OPTIONS: Array<{
  value: RuntimeAutonomyOperation;
  label: string;
}> = [
  { value: "automatic_resume", label: "故障恢复后自动续跑" },
  { value: "model_call", label: "调用指定模型" },
  { value: "local_read", label: "读取当前工作区" },
  { value: "local_write_new", label: "在当前工作区新建文件" },
  { value: "local_modify_existing", label: "修改当前工作区文件" },
  { value: "network_read", label: "联网读取" },
  { value: "gui", label: "操作图形界面" },
  { value: "external_delivery", label: "向外部系统交付" },
];

const RESUME_OPTIONS: Array<{
  value: RuntimeAutoResumeCondition;
  label: string;
}> = [
  { value: "backend_restart", label: "后端重启" },
  { value: "app_reopen", label: "应用重新打开" },
  { value: "network_recovery", label: "网络恢复" },
];

function cnyToMicroCny(value: number): number {
  return Math.round(value * 1_000_000);
}

export default function AutonomyGrantModal({
  item,
  open,
  busy,
  onCancel,
  onSubmit,
}: AutonomyGrantModalProps) {
  const [operations, setOperations] =
    useState<RuntimeAutonomyOperation[]>(DEFAULT_OPERATIONS);
  const [resumeConditions, setResumeConditions] =
    useState<RuntimeAutoResumeCondition[]>(DEFAULT_RESUME_CONDITIONS);
  const [durationHours, setDurationHours] = useState(8);
  const [providerId, setProviderId] = useState("");
  const [modelId, setModelId] = useState("");
  const [pricingVersion, setPricingVersion] = useState("");
  const [taskCapCny, setTaskCapCny] = useState<number | null>(null);
  const [dispatchCapCny, setDispatchCapCny] = useState<number | null>(null);
  const [priceConfirmed, setPriceConfirmed] = useState(false);
  const [reason, setReason] = useState("用户在任务详情中明确授权后台续跑");
  const [validationError, setValidationError] = useState("");

  useEffect(() => {
    if (!open) return;
    setOperations(DEFAULT_OPERATIONS);
    setResumeConditions(DEFAULT_RESUME_CONDITIONS);
    setDurationHours(8);
    setProviderId("");
    setModelId("");
    setPricingVersion("");
    setTaskCapCny(null);
    setDispatchCapCny(null);
    setPriceConfirmed(false);
    setReason("用户在任务详情中明确授权后台续跑");
    setValidationError("");
  }, [item?.id, open]);

  const usesModel = operations.includes("model_call");
  const usesLocalFiles = operations.some((operation) =>
    LOCAL_OPERATIONS.includes(operation),
  );
  const autoResumes = operations.includes("automatic_resume");

  const scopeSummary = useMemo(() => {
    if (!item) return "";
    return `仅任务 ${item.display_id || item.id.slice(0, 8)}；文件范围仅 ${item.workspace_dir || "未绑定"}`;
  }, [item]);

  const submit = async () => {
    if (!item) return;
    if (!operations.length) {
      setValidationError("请至少选择一种允许的动作。");
      return;
    }
    if (usesLocalFiles && !item.workspace_dir) {
      setValidationError("当前任务没有可验证的工作区路径，不能授权文件操作。");
      return;
    }
    if (autoResumes && !resumeConditions.length) {
      setValidationError("自动续跑必须至少绑定一种恢复条件。");
      return;
    }
    if (
      usesModel &&
      (!providerId.trim() ||
        !modelId.trim() ||
        !pricingVersion.trim() ||
        !taskCapCny ||
        taskCapCny <= 0 ||
        !dispatchCapCny ||
        dispatchCapCny <= 0 ||
        !priceConfirmed)
    ) {
      setValidationError(
        "模型调用需要精确 Provider、模型、价格版本、任务上限、单次上限，并确认价格上界已核验。",
      );
      return;
    }
    if (
      usesModel &&
      taskCapCny !== null &&
      dispatchCapCny !== null &&
      dispatchCapCny > taskCapCny
    ) {
      setValidationError("单次调用上限不能高于整个任务上限。");
      return;
    }
    if (!reason.trim()) {
      setValidationError("请说明本次授权的原因。");
      return;
    }

    const request: RuntimeAutonomyGrantRequest = {
      approved: true,
      expected_attempt_id: item.attempt_id,
      expected_revision: item.revision,
      chat_id: item.chat_id,
      session_id: item.session_id,
      workspace_dir: item.workspace_dir,
      expires_at: new Date(
        Date.now() + durationHours * 60 * 60 * 1_000,
      ).toISOString(),
      allowed_operations: operations,
      allowed_paths:
        usesLocalFiles && item.workspace_dir ? [item.workspace_dir] : [],
      allowed_provider_ids: usesModel ? [providerId.trim()] : [],
      allowed_model_ids: usesModel ? [modelId.trim()] : [],
      allow_network: operations.includes("network_read"),
      allow_gui: operations.includes("gui"),
      allow_external_delivery: operations.includes("external_delivery"),
      auto_resume_conditions: autoResumes ? resumeConditions : [],
      budget_currency: usesModel ? "CNY" : null,
      max_cost_micro_cny:
        usesModel && taskCapCny !== null ? cnyToMicroCny(taskCapCny) : null,
      model_price_bounds:
        usesModel && dispatchCapCny !== null
          ? [
              {
                provider_id: providerId.trim(),
                model_id: modelId.trim(),
                pricing_version: pricingVersion.trim(),
                max_dispatch_micro_cny: cnyToMicroCny(dispatchCapCny),
                upper_bound_proven: true,
              },
            ]
          : [],
      reason: reason.trim(),
    };
    setValidationError("");
    if (await onSubmit(item, request)) onCancel();
  };

  return (
    <Modal
      title="授权后台续跑"
      open={open}
      width={680}
      okText="确认授权"
      cancelText="取消"
      confirmLoading={busy}
      maskClosable={!busy}
      closable={!busy}
      onCancel={onCancel}
      onOk={() => void submit()}
    >
      <div className={styles.autonomyModalBody}>
        <Alert
          type="warning"
          showIcon
          message="这是一次有期限、可撤销、仅属于当前任务的授权"
          description={scopeSummary}
        />
        {validationError ? (
          <Alert type="error" showIcon message={validationError} />
        ) : null}

        <Form layout="vertical" requiredMark>
          <Form.Item label="授权有效期">
            <Select
              aria-label="授权有效期"
              value={durationHours}
              onChange={setDurationHours}
              options={[
                { value: 1, label: "1 小时" },
                { value: 2, label: "2 小时" },
                { value: 8, label: "8 小时" },
              ]}
            />
          </Form.Item>

          <Form.Item label="允许的动作">
            <Checkbox.Group
              className={styles.autonomyCheckGrid}
              value={operations}
              options={OPERATION_OPTIONS}
              onChange={(values) =>
                setOperations(values as RuntimeAutonomyOperation[])
              }
            />
          </Form.Item>

          {autoResumes ? (
            <Form.Item label="可自动续跑的恢复条件">
              <Checkbox.Group
                className={styles.autonomyCheckGrid}
                value={resumeConditions}
                options={RESUME_OPTIONS}
                onChange={(values) =>
                  setResumeConditions(values as RuntimeAutoResumeCondition[])
                }
              />
            </Form.Item>
          ) : null}

          {usesLocalFiles ? (
            <Form.Item label="文件范围">
              <Input
                aria-label="文件范围"
                readOnly
                value={item?.workspace_dir || ""}
                status={item?.workspace_dir ? undefined : "error"}
              />
            </Form.Item>
          ) : null}

          {usesModel ? (
            <div className={styles.autonomyModelFields}>
              <Form.Item label="Provider ID" required>
                <Input
                  aria-label="Provider ID"
                  value={providerId}
                  onChange={(event) => setProviderId(event.target.value)}
                  placeholder="例如 openai"
                />
              </Form.Item>
              <Form.Item label="Model ID" required>
                <Input
                  aria-label="Model ID"
                  value={modelId}
                  onChange={(event) => setModelId(event.target.value)}
                  placeholder="填写完整模型 ID"
                />
              </Form.Item>
              <Form.Item label="价格版本" required>
                <Input
                  aria-label="价格版本"
                  value={pricingVersion}
                  onChange={(event) => setPricingVersion(event.target.value)}
                  placeholder="例如 2026-09-20"
                />
              </Form.Item>
              <Form.Item label="任务总上限（人民币元）" required>
                <InputNumber
                  aria-label="任务总上限（人民币元）"
                  min={0.000001}
                  precision={6}
                  value={taskCapCny}
                  onChange={(value) => setTaskCapCny(value)}
                  className={styles.fullWidthControl}
                />
              </Form.Item>
              <Form.Item label="单次调用上限（人民币元）" required>
                <InputNumber
                  aria-label="单次调用上限（人民币元）"
                  min={0.000001}
                  precision={6}
                  value={dispatchCapCny}
                  onChange={(value) => setDispatchCapCny(value)}
                  className={styles.fullWidthControl}
                />
              </Form.Item>
              <Form.Item className={styles.autonomyPriceConfirmation}>
                <Checkbox
                  checked={priceConfirmed}
                  onChange={(event) => setPriceConfirmed(event.target.checked)}
                >
                  我已按上述价格版本核验该 Provider / Model
                  的单次调用最坏费用上界。
                </Checkbox>
              </Form.Item>
            </div>
          ) : null}

          <Form.Item label="授权原因" required>
            <Input.TextArea
              aria-label="授权原因"
              value={reason}
              rows={2}
              maxLength={500}
              onChange={(event) => setReason(event.target.value)}
            />
          </Form.Item>
        </Form>
      </div>
    </Modal>
  );
}
