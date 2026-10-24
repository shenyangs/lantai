import { Button, Drawer, Form, Input, Select, Switch } from "antd";
import { useEffect, useState } from "react";

import type { CreateRuntimeTaskRequest } from "../../../api/types/runtime";

interface CreateTaskDrawerProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onCreate: (value: CreateRuntimeTaskRequest) => Promise<void>;
}

interface CreateTaskFormValue {
  title?: string;
  objective: string;
  acceptanceText?: string;
  work_method_id: string;
  auto_start: boolean;
}

function makeClientTaskId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return `task-${crypto.randomUUID()}`;
  }
  return `task-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function CreateTaskDrawer({
  open,
  busy,
  onClose,
  onCreate,
}: CreateTaskDrawerProps) {
  const [form] = Form.useForm<CreateTaskFormValue>();
  const [clientTaskId, setClientTaskId] = useState(makeClientTaskId);

  useEffect(() => {
    if (open) setClientTaskId(makeClientTaskId());
  }, [open]);

  const submit = async () => {
    const value = await form.validateFields();
    await onCreate({
      title: value.title?.trim() || null,
      objective: value.objective.trim(),
      acceptance: String(value.acceptanceText || "")
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean),
      work_method_id: value.work_method_id,
      client_task_id: clientTaskId,
      auto_start: value.auto_start,
    });
    form.resetFields();
  };

  return (
    <Drawer
      title="创建任务"
      width={480}
      open={open}
      onClose={onClose}
      destroyOnClose
      extra={
        <Button type="primary" loading={busy} onClick={() => void submit()}>
          保存任务
        </Button>
      }
    >
      <Form<CreateTaskFormValue>
        form={form}
        layout="vertical"
        initialValues={{ work_method_id: "standard", auto_start: false }}
      >
        <Form.Item label="标题" name="title">
          <Input maxLength={160} placeholder="可选；留空时使用目标首行" />
        </Form.Item>
        <Form.Item
          label="任务目标"
          name="objective"
          rules={[{ required: true, message: "请说明要完成什么" }]}
        >
          <Input.TextArea
            autoSize={{ minRows: 5, maxRows: 12 }}
            maxLength={20_000}
            showCount
            placeholder="写清目标、范围和最终希望拿到的结果。"
          />
        </Form.Item>
        <Form.Item
          label="验收条件"
          name="acceptanceText"
          extra="每行一条。任务完成时会保留在运行契约中。"
        >
          <Input.TextArea
            autoSize={{ minRows: 3, maxRows: 8 }}
            placeholder={"例如：\n构建通过\n关键路径有测试覆盖"}
          />
        </Form.Item>
        <Form.Item label="工作方式" name="work_method_id">
          <Select
            options={[
              { value: "standard", label: "标准执行" },
              { value: "deep_research", label: "深度研究" },
              { value: "sustained_execution", label: "持续执行" },
              { value: "collaboration", label: "协作" },
            ]}
          />
        </Form.Item>
        <Form.Item
          label="保存后立即开始"
          name="auto_start"
          valuePropName="checked"
          extra="关闭时任务会进入“待开始”，之后可从卡片启动。"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Drawer>
  );
}
