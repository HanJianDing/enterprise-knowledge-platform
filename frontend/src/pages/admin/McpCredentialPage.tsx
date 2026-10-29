/** 外部接入管理页（批次D）：长期 MCP 接入凭证——"发钥匙的柜台"
 *
 * 接入方（Claude Desktop/公司其他 AI 应用）凭凭证连 MCP 端点，
 * 凭证可限定可见库范围、可随时撤销；原文只在创建时展示一次。
 */
import React, { useEffect, useState } from "react";
import {
  Card, Table, Button, Tag, Modal, Form, Input, Select, Typography, Alert, Popconfirm,
} from "antd";
import { PlusOutlined, StopOutlined, CopyOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { mcpCredentialApi, type McpCredentialItem } from "../../services/adminApi";
import { kbApi } from "../../services/kbApi";
import type { KnowledgeBaseInfo } from "../../types/kb";

const { Text, Paragraph } = Typography;

const McpCredentialPage: React.FC = () => {
  const [items, setItems] = useState<McpCredentialItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBaseInfo[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [form] = Form.useForm();

  const fetchList = async () => {
    setLoading(true);
    try {
      setItems((await mcpCredentialApi.list()).items);
    } catch {
      message.error("加载接入凭证失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchList();
    kbApi.listKbs().then(setKbs).catch(() => {});
  }, []);

  const handleCreate = async () => {
    try {
      const values = await form.validateFields();
      setCreating(true);
      const r = await mcpCredentialApi.create({
        name: values.name,
        kb_ids: values.kb_ids?.length ? values.kb_ids : null,
      });
      setCreatedToken(r.token);
      setCreateOpen(false);
      form.resetFields();
      fetchList();
    } catch (err: any) {
      if (err?.response?.data?.detail) message.error(err.response.data.detail);
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (id: string) => {
    try {
      const r = await mcpCredentialApi.revoke(id);
      message.success(r.message);
      fetchList();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "撤销失败");
    }
  };

  const columns: ColumnsType<McpCredentialItem> = [
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "可见库范围", dataIndex: "kb_ids", key: "kb",
      render: (ids: string[] | null) =>
        ids && ids.length > 0
          ? ids.slice(0, 3).map((id) => {
              const kb = kbs.find((k) => k.id === id);
              return <Tag key={id} color="blue">{kb?.name || id.slice(0, 8)}</Tag>;
            })
          : <Tag>全部可见库</Tag>,
    },
    { title: "绑定账号", dataIndex: "owner_username", key: "owner", width: 120 },
    {
      title: "状态", dataIndex: "revoked", key: "revoked", width: 100,
      render: (revoked: boolean) => revoked
        ? <Tag color="red">已撤销</Tag>
        : <Tag color="green">有效</Tag>,
    },
    {
      title: "创建时间", dataIndex: "created_at", key: "time", width: 170,
      render: (t: string | null) => (t ? new Date(t).toLocaleString("zh-CN") : "-"),
    },
    {
      title: "操作", key: "action", width: 90,
      render: (_, record) =>
        record.revoked ? null : (
          <Popconfirm
            title={`撤销「${record.name}」后接入方立即失效，确认？`}
            okText="撤销" cancelText="取消"
            onConfirm={() => handleRevoke(record.id)}
          >
            <Button type="link" danger size="small" icon={<StopOutlined />}>撤销</Button>
          </Popconfirm>
        ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="外部接入" description="为其他 AI 系统签发长期接入凭证（MCP 集成），可限库、可撤销" />
      <Card
        title="接入凭证"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建接入凭证
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="接入方式：对方在 MCP 客户端配置地址 http(s)://<部署地址>/mcp/ 与凭证即可调用知识库检索，权限=绑定账号可见范围 ∩ 凭证库白名单。"
        />
        <Table columns={columns} dataSource={items} rowKey="id" loading={loading} pagination={false} />
      </Card>

      {/* 新建弹窗 */}
      <Modal
        title="新建接入凭证"
        open={createOpen}
        onOk={handleCreate}
        onCancel={() => { setCreateOpen(false); form.resetFields(); }}
        okText="创建" cancelText="取消" confirmLoading={creating}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="接入方名称" rules={[{ required: true, message: "请输入接入方名称" }]}>
            <Input placeholder="例如：OA 系统、Claude Desktop" />
          </Form.Item>
          <Form.Item name="kb_ids" label="可见知识库（留空=全部可见库）">
            <Select
              mode="multiple"
              allowClear
              placeholder="限定可见库范围（可选）"
              options={kbs.map((k) => ({ label: k.name, value: k.id }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 一次性凭证展示 */}
      <Modal
        title="凭证创建成功"
        open={!!createdToken}
        onCancel={() => setCreatedToken(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            icon={<CopyOutlined />}
            onClick={() => {
              if (createdToken) navigator.clipboard.writeText(createdToken);
              message.success("已复制到剪贴板");
            }}
          >
            复制凭证
          </Button>,
          <Button key="done" onClick={() => setCreatedToken(null)}>我已保存</Button>,
        ]}
      >
        <Alert
          type="warning"
          showIcon
          message="凭证原文只展示这一次，关闭后无法再次查看"
          style={{ marginBottom: 12 }}
        />
        <Paragraph copyable={{ text: createdToken || "" }} style={{ wordBreak: "break-all" }}>
          <Text code>{createdToken}</Text>
        </Paragraph>
        <Text type="secondary">接入方配置：Authorization: Bearer 上述凭证</Text>
      </Modal>
    </div>
  );
};

export default McpCredentialPage;
