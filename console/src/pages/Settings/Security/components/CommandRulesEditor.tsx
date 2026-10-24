import { Button, Select, Switch } from "@/vendor/lantaiDesign";
import { PlusCircleOutlined } from "@ant-design/icons";
import { Input, Table } from "antd";
import type { ToolGuardCommandRule } from "../../../../api/modules/security";
import styles from "../index.module.less";

interface CommandRulesEditorProps {
  enabled: boolean;
  toolOptions: { label: string; value: string }[];
  commandRules: ToolGuardCommandRule[];
  setCommandRules: (rules: ToolGuardCommandRule[]) => void;
}

const EFFECT_OPTIONS = [
  { label: "Allow（始终允许）", value: "allow" },
  { label: "Ask（每次询问）", value: "ask" },
  { label: "Deny（始终拒绝）", value: "deny" },
];

export function CommandRulesEditor({
  enabled,
  toolOptions,
  commandRules,
  setCommandRules,
}: CommandRulesEditorProps) {
  const updateCommandRule = (
    ruleId: string,
    patch: Partial<ToolGuardCommandRule>,
  ) => {
    setCommandRules(
      commandRules.map((rule) =>
        rule.id === ruleId ? { ...rule, ...patch } : rule,
      ),
    );
  };

  const addCommandRule = () => {
    const id = `command-rule-${Date.now()}`;
    setCommandRules([
      ...commandRules,
      {
        id,
        enabled: true,
        effect: "allow",
        tool_name: "execute_shell_command",
        command_pattern: "",
        path_pattern: "",
        description: "",
        source: "user",
      },
    ]);
  };

  const removeCommandRule = (ruleId: string) => {
    setCommandRules(commandRules.filter((rule) => rule.id !== ruleId));
  };

  const commandRuleColumns = [
    {
      title: "动作",
      dataIndex: "effect",
      width: 180,
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Select
          aria-label={`${record.id}：动作`}
          value={record.effect}
          options={EFFECT_OPTIONS}
          disabled={!enabled}
          onChange={(value) =>
            updateCommandRule(record.id, {
              effect: value as ToolGuardCommandRule["effect"],
            })
          }
        />
      ),
    },
    {
      title: "工具",
      dataIndex: "tool_name",
      width: 220,
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Select
          aria-label={`${record.id}：工具`}
          value={record.tool_name}
          options={[{ label: "*", value: "*" }, ...toolOptions]}
          disabled={!enabled}
          showSearch
          onChange={(value) =>
            updateCommandRule(record.id, { tool_name: value })
          }
        />
      ),
    },
    {
      title: "命令正则",
      dataIndex: "command_pattern",
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Input
          aria-label={`${record.id}：命令正则`}
          value={record.command_pattern}
          placeholder="例如 ^npm\\s+install\\b"
          disabled={!enabled}
          onChange={(event) =>
            updateCommandRule(record.id, {
              command_pattern: event.target.value,
            })
          }
        />
      ),
    },
    {
      title: "路径模式",
      dataIndex: "path_pattern",
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Input
          aria-label={`${record.id}：路径模式`}
          value={record.path_pattern}
          placeholder="/Users/name/Projects/**"
          disabled={!enabled}
          onChange={(event) =>
            updateCommandRule(record.id, {
              path_pattern: event.target.value,
            })
          }
        />
      ),
    },
    {
      title: "说明",
      dataIndex: "description",
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Input
          aria-label={`${record.id}：说明`}
          value={record.description}
          placeholder="给审批横幅看的说明"
          disabled={!enabled}
          onChange={(event) =>
            updateCommandRule(record.id, {
              description: event.target.value,
            })
          }
        />
      ),
    },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 86,
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Switch
          aria-label={`${record.id}：${record.enabled ? "禁用" : "启用"}`}
          checked={record.enabled}
          disabled={!enabled}
          onChange={(checked) =>
            updateCommandRule(record.id, { enabled: checked })
          }
        />
      ),
    },
    {
      title: "",
      key: "actions",
      width: 72,
      render: (_: unknown, record: ToolGuardCommandRule) => (
        <Button
          type="text"
          danger
          aria-label={`删除命令规则：${record.id}`}
          disabled={!enabled}
          onClick={() => removeCommandRule(record.id)}
        >
          删除
        </Button>
      ),
    },
  ];

  return (
    <>
      <div className={styles.sectionHeader}>
        <h2 className={styles.sectionTitle}>命令权限规则</h2>
        <Button
          type="primary"
          icon={<PlusCircleOutlined />}
          onClick={addCommandRule}
          disabled={!enabled}
          size="middle"
        >
          添加规则
        </Button>
      </div>

      <Table
        rowKey="id"
        columns={commandRuleColumns}
        dataSource={commandRules}
        pagination={false}
        scroll={{ x: 1080 }}
        locale={{ emptyText: "暂无命令规则" }}
      />
    </>
  );
}
