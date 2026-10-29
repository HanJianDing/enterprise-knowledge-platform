import React, { useRef, useState } from "react";
import { List, Button, Popconfirm, Typography, Input, Modal } from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  MessageOutlined,
  PlusOutlined,
  ClearOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import type { Conversation } from "../../types/chat";
import { useThemeTokens } from "../../theme/themeStore";

const { Text } = Typography;

interface Props {
  conversations: Conversation[];
  currentId: string | null;
  collapsed: boolean;
  loading?: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNewChat: () => void;
  onRename: (id: string, title: string) => void;
  onClearAll: () => void;
}

const ConversationSidebar: React.FC<Props> = ({
  conversations,
  currentId,
  collapsed,
  loading = false,
  onSelect,
  onDelete,
  onNewChat,
  onRename,
  onClearAll,
}) => {
  const [search, setSearch] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const t = useThemeTokens();
  // 防双触发：Enter 提交与 blur 可能在状态更新前的同一轮里接连触发，
  // 同一编辑会话只允许提交一次（重复 PATCH 虽幂等但没必要）
  const renameCommittedRef = useRef<string | null>(null);

  const filtered = conversations.filter((c) =>
    c.title.toLowerCase().includes(search.trim().toLowerCase())
  );

  const startRename = (conv: Conversation, e: React.MouseEvent) => {
    e.stopPropagation();
    renameCommittedRef.current = null;
    setEditingId(conv.id);
    setEditingTitle(conv.title);
  };

  const commitRename = (id: string) => {
    if (renameCommittedRef.current === id) return;
    renameCommittedRef.current = id;
    setEditingId(null);
    if (editingTitle.trim() && editingTitle.trim() !== conversations.find((c) => c.id === id)?.title) {
      onRename(id, editingTitle);
    }
  };

  const confirmClearAll = () => {
    Modal.confirm({
      title: "确定清空全部历史对话？",
      content: "将删除全部历史对话，此操作不可恢复。进行中的人工服务会话会保留。",
      okText: "清空",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: onClearAll,
    });
  };

  if (collapsed) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ padding: "16px", borderBottom: `1px solid ${t.color.borderBase}` }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={onNewChat}
          block
          size="large"
        >
          新建对话
        </Button>
        <Input
          allowClear
          size="middle"
          prefix={<SearchOutlined style={{ color: t.color.textTertiary }} />}
          placeholder="搜索历史对话..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ marginTop: 10 }}
        />
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        <List
          className="conversation-list"
          loading={loading}
          dataSource={filtered}
          renderItem={(item) => (
            <List.Item
              onClick={() => onSelect(item.id)}
              style={{
                cursor: "pointer",
                padding: "12px 16px",
                background: item.id === currentId ? t.color.primaryBg : "transparent",
                borderLeft:
                  item.id === currentId ? `3px solid ${t.color.primary}` : "3px solid transparent",
              }}
              actions={
                editingId === item.id
                  ? []
                  : [
                      <Button
                        key="rename"
                        type="text"
                        size="small"
                        icon={<EditOutlined />}
                        title="重命名"
                        onClick={(e) => startRename(item, e)}
                      />,
                      <Popconfirm
                        key="del"
                        title="确定删除此对话?"
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          onDelete(item.id);
                        }}
                        onCancel={(e) => e?.stopPropagation()}
                      >
                        <Button
                          type="text"
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>,
                    ]
              }
            >
              <List.Item.Meta
                avatar={<MessageOutlined style={{ color: t.color.primary }} />}
                title={
                  editingId === item.id ? (
                    <Input
                      size="small"
                      autoFocus
                      value={editingTitle}
                      maxLength={50}
                      onChange={(e) => setEditingTitle(e.target.value)}
                      onPressEnter={() => commitRename(item.id)}
                      onBlur={() => commitRename(item.id)}
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    <Text
                      ellipsis={{ tooltip: item.title }}
                      style={{
                        fontWeight: item.id === currentId ? 600 : 400,
                        fontSize: 14,
                      }}
                    >
                      {item.title}
                    </Text>
                  )
                }
                description={
                  <Text style={{ fontSize: 12, color: t.color.textTertiary }}>
                    {item.message_count} 条消息 ·{" "}
                    {new Date(item.updated_at).toLocaleDateString("zh-CN")}
                  </Text>
                }
              />
            </List.Item>
          )}
          locale={{ emptyText: loading ? "加载会话中…" : search ? "没有匹配的对话" : "暂无对话" }}
        />
      </div>
      <div style={{ padding: "12px 16px", borderTop: `1px solid ${t.color.borderBase}` }}>
        <Button
          danger
          block
          icon={<ClearOutlined />}
          onClick={confirmClearAll}
          disabled={conversations.length === 0}
        >
          清空全部对话
        </Button>
      </div>
    </div>
  );
};

export default ConversationSidebar;
