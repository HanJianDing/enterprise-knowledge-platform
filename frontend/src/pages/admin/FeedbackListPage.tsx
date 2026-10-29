import React, { useEffect, useState } from "react";
import { Card, Table, Tag, Select, Space, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { adminApi, type FeedbackItem } from "../../services/adminApi";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";

const { Text } = Typography;

/** 用户对 AI 回答的点赞/点踩明细（管理端"满意度反馈"页） */
const FeedbackListPage: React.FC = () => {
  const [items, setItems] = useState<FeedbackItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [rating, setRating] = useState<"like" | "dislike" | undefined>(undefined);
  const [hasComment, setHasComment] = useState<boolean | undefined>(undefined);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    adminApi
      .getFeedbackList({
        page,
        page_size: pageSize,
        rating,
        has_comment: hasComment,
      })
      .then((r) => {
        setItems(r.items);
        setTotal(r.total);
      })
      .catch(() => message.error("加载反馈列表失败"))
      .finally(() => setLoading(false));
  }, [page, pageSize, rating, hasComment]);

  const columns: ColumnsType<FeedbackItem> = [
    {
      title: "用户",
      dataIndex: "username",
      width: 120,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: "评价",
      dataIndex: "rating",
      width: 80,
      render: (v: string) =>
        v === "like" ? (
          <Tag color="green">👍 点赞</Tag>
        ) : (
          <Tag color="red">👎 点踩</Tag>
        ),
    },
    {
      title: "会话",
      dataIndex: "conversation_title",
      width: 160,
      ellipsis: true,
    },
    {
      title: "AI 回答摘要",
      dataIndex: "message_content",
      ellipsis: true,
    },
    {
      title: "意见",
      dataIndex: "comment",
      width: 220,
      ellipsis: true,
      // 语义：只有点踩才可能填意见；点赞行显示"—"（不适用），点踩行无意见才显示"（未填写）"
      render: (v: string | null, record: FeedbackItem) =>
        record.rating === "like" ? (
          <Text type="secondary">—</Text>
        ) : v ? (
          <Text type="danger">{v}</Text>
        ) : (
          <Text type="secondary">（未填写）</Text>
        ),
    },
    {
      title: "时间",
      dataIndex: "created_at",
      width: 170,
      render: (v: string) => new Date(v).toLocaleString("zh-CN"),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="满意度反馈" description="用户对 AI 回答的点赞/点踩与意见明细" />
      <Card>
        <Space style={{ marginBottom: 16 }}>
          <Select
            allowClear
            placeholder="全部评价"
            style={{ width: 130 }}
            value={rating}
            onChange={(v) => {
              setRating(v);
              setPage(1);
            }}
            options={[
              { value: "like", label: "👍 点赞" },
              { value: "dislike", label: "👎 点踩" },
            ]}
          />
          <Select
            allowClear
            placeholder="是否填了意见"
            style={{ width: 150 }}
            value={hasComment}
            onChange={(v) => {
              setHasComment(v);
              setPage(1);
            }}
            options={[
              { value: true, label: "填了意见" },
              { value: false, label: "未填意见" },
            ]}
          />
        </Space>
        <Table<FeedbackItem>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </Card>
    </div>
  );
};

export default FeedbackListPage;
