import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Form, Modal, Table, Button } from "@/vendor/lantaiDesign";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useTranslation } from "react-i18next";
import {
  createColumns,
  FilterBar,
  SessionDrawer,
  type Session,
} from "./components";
import { useSessions } from "./useSessions";
import api from "../../../api";
import { PageHeader } from "@/components/PageHeader";
import styles from "./index.module.less";

function SessionsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    sessions,
    loading,
    updateSession,
    deleteSession,
    batchDeleteSessions,
  } = useSessions();
  const [filteredSessions, setFilteredSessions] = useState<Session[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingSession, setEditingSession] = useState<Session | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<Session>();

  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  // Filter states
  const [filterUserId, setFilterUserId] = useState<string>("");
  const [filterChannel, setFilterChannel] = useState<string>("");
  const [availableChannels, setAvailableChannels] = useState<string[]>([]);

  const { message } = useAppMessage();

  useEffect(() => {
    const fetchChannelTypes = async () => {
      try {
        const types = await api.listChannelTypes();
        setAvailableChannels(types);
      } catch (error) {
        console.error("❌ Failed to load channel types:", error);
      }
    };
    fetchChannelTypes();
  }, []);

  // Filter effect
  useEffect(() => {
    let filtered: Session[] = sessions;

    if (filterUserId) {
      const query = filterUserId.toLowerCase();
      filtered = filtered.filter((session: Session) =>
        [
          session.name,
          session.channel,
          session.user_id,
          session.session_id,
          session.id,
        ]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(query)),
      );
    }

    if (filterChannel) {
      filtered = filtered.filter(
        (session: Session) => session.channel === filterChannel,
      );
    }

    setFilteredSessions(filtered);
  }, [sessions, filterUserId, filterChannel]);

  const handleEdit = (session: Session) => {
    setEditingSession(session);
    form.setFieldsValue(session as any);
    setDrawerOpen(true);
  };

  const handleDelete = (sessionId: string) => {
    Modal.confirm({
      title: t("sessions.confirmDelete"),
      content: t("sessions.deleteConfirm"),
      okText: t("cronJobs.deleteText"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await deleteSession(sessionId);
      },
    });
  };

  const handleView = (session: Session) => {
    navigate(`/chat/${encodeURIComponent(session.id)}`);
  };

  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) {
      message.warning(t("sessions.batchDeleteConfirm", { count: 0 }));
      return;
    }

    Modal.confirm({
      title: t("sessions.confirmDelete"),
      content: t("sessions.batchDeleteConfirm", {
        count: selectedRowKeys.length,
      }),
      okText: t("cronJobs.deleteText"),
      okType: "danger",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        const success = await batchDeleteSessions(selectedRowKeys as string[]);
        if (success) {
          setSelectedRowKeys([]);
        }
      },
    });
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
    setEditingSession(null);
  };

  const handleSubmit = async (values: Session) => {
    if (editingSession) {
      setSaving(true);
      try {
        const updated = {
          name: values.name,
        };
        const success = await updateSession(editingSession.id, updated);
        if (success) {
          setDrawerOpen(false);
        }
      } finally {
        setSaving(false);
      }
    }
  };

  const columns = createColumns({
    onEdit: handleEdit,
    onDelete: handleDelete,
    onView: handleView,
    t,
  });

  const rowSelection = {
    fixed: true,
    columnWidth: 50,
    selectedRowKeys,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedRowKeys(newSelectedRowKeys);
    },
  };

  const runningCount = filteredSessions.filter(
    (session) => session.status === "running",
  ).length;
  const channelCount = new Set(
    filteredSessions.map((session) => session.channel || "default"),
  ).size;

  return (
    <div className={styles.sessionsPage}>
      <PageHeader
        items={[{ title: t("nav.control") }, { title: t("sessions.title") }]}
        description={t("sessions.description")}
        extra={
          <div className={styles.headerRight}>
            <FilterBar
              filterUserId={filterUserId}
              filterChannel={filterChannel}
              uniqueChannels={availableChannels}
              onUserIdChange={setFilterUserId}
              onChannelChange={setFilterChannel}
            />
            {selectedRowKeys.length > 0 && (
              <Button type="primary" danger onClick={handleBatchDelete}>
                {t("sessions.batchDeleteButton")} ({selectedRowKeys.length})
              </Button>
            )}
          </div>
        }
      />

      <Card className={styles.tableCard} styles={{ body: { padding: 0 } }}>
        <div className={styles.sessionsTableHeader}>
          <div>
            <h3>会话管理</h3>
            <p>
              主列表只展示名称、来源和时间；内部 ID 收进编辑详情，排障时再复制。
            </p>
          </div>
          <div className={styles.sessionSummaryRail}>
            <span>
              <strong>{filteredSessions.length}</strong> 个会话
            </span>
            <span>
              <strong>{channelCount}</strong> 个来源
            </span>
            <span>
              <strong>{runningCount}</strong> 个进行中
            </span>
          </div>
        </div>
        <Table
          columns={columns}
          dataSource={filteredSessions}
          loading={loading}
          rowKey="id"
          rowSelection={rowSelection}
          rowClassName={(record) =>
            selectedRowKeys.includes(record.id) ? styles.selectedRow : ""
          }
          scroll={{ x: 980 }}
          pagination={{
            pageSize: 10,
            showSizeChanger: false,
          }}
        />
      </Card>

      <SessionDrawer
        open={drawerOpen}
        editingSession={editingSession}
        form={form}
        saving={saving}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />
    </div>
  );
}

export default SessionsPage;
