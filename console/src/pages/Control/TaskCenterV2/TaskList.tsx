import { Button, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";

import type { RuntimeTaskBoardItem } from "../../../api/types/runtime";
import { taskSourceLabel, taskStatusLabel } from "./taskBoardModel";
import styles from "./index.module.less";

interface TaskListProps {
  items: RuntimeTaskBoardItem[];
  onOpen: (item: RuntimeTaskBoardItem) => void;
}

export default function TaskList({ items, onOpen }: TaskListProps) {
  const columns: ColumnsType<RuntimeTaskBoardItem> = [
    {
      title: "任务",
      dataIndex: "title",
      key: "title",
      render: (_, item) => (
        <button
          type="button"
          className={styles.listTitle}
          onClick={() => onOpen(item)}
        >
          <strong>{item.title}</strong>
          <span>{item.summary || "暂无摘要"}</span>
        </button>
      ),
    },
    {
      title: "状态",
      key: "lane",
      width: 118,
      render: (_, item) => (
        <span className={styles.statusText} data-lane={item.lane}>
          {taskStatusLabel(item)}
        </span>
      ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 92,
      render: (value) => taskSourceLabel(String(value)),
    },
    {
      title: "最近更新",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 144,
      render: (value) =>
        dayjs(value).isValid() ? dayjs(value).format("MM/DD HH:mm") : "—",
    },
    {
      title: "",
      key: "action",
      width: 76,
      render: (_, item) => (
        <Button type="link" onClick={() => onOpen(item)}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <Table<RuntimeTaskBoardItem>
      className={styles.taskTable}
      rowKey="id"
      columns={columns}
      dataSource={items}
      pagination={{ pageSize: 20, hideOnSinglePage: true }}
      locale={{ emptyText: "这个视图里还没有任务" }}
      scroll={{ x: 720 }}
    />
  );
}
