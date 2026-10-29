/** 审计日志页（企业知识库改造 阶段3）
 *
 * "谁在什么时候动了什么，全部留痕可查"——按操作/用户名/资源类型过滤。
 */
import React, { useEffect, useState } from "react";
import { Card, Table, Input, Select, Button, Space, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined, SearchOutlined, DownloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi } from "../../services/adminApi";
import type { AuditLogItem } from "../../services/adminApi";

const { Text } = Typography;

/** 操作名 → 中文标签 */
const ACTION_LABEL: Record<string, string> = {
  "user.login": "登录",
  "user.change_password": "修改密码",
  "user.create": "创建账号",
  "user.import": "批量导入账号",
  "user.role_change": "修改角色",
  "user.status_change": "启用/禁用用户",
  "user.departments_set": "设置所属部门",
  "kb.create": "创建知识库",
  "kb.update": "修改知识库",
  "kb.delete": "删除知识库",
  "kb.members_update": "修改库授权",
  "department.create": "创建部门",
  "department.update": "修改部门",
  "department.delete": "删除部门",
  "document.upload": "上传文档",
  "document.delete": "删除文档",
  "category.create": "创建分类",
  "category.delete": "删除分类",
  "document.reprocess": "文档重试/重切",
  "user.password_reset": "重置密码",
  "eval.run": "评估跑分",
  "mcp.search_kb": "MCP 检索调用",
  "mcp.list_kbs": "MCP 库列表调用",
  "mcp.tool_call": "外部工具调用",
  "mcp.credential_create": "创建接入凭证",
  "mcp.credential_revoke": "撤销接入凭证",
  "user.sso_create": "SSO 自动建号",
  "user.sso_login": "SSO 登录",
  "tool.call": "内置工具调用",
};

const AUDIT_ACTION_OPTIONS = Object.entries(ACTION_LABEL).map(([value, label]) => ({
  value, label: `${label}（${value}）`,
}));

const AuditLogPage: React.FC = () => {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [actionFilter, setActionFilter] = useState<string | undefined>();
  const [usernameFilter, setUsernameFilter] = useState<string | undefined>();
  const [resourceTypeFilter, setResourceTypeFilter] = useState<string | undefined>();

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const r = await adminApi.getAuditLogs({
        page,
        page_size: 20,
        action: actionFilter,
        username: usernameFilter || undefined,
        resource_type: resourceTypeFilter,
      });
      setItems(r.items);
      setTotal(r.total);
    } catch {
      message.error("加载审计日志失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchLogs(); }, [page, actionFilter, resourceTypeFilter]);

  const columns: ColumnsType<AuditLogItem> = [
    {
      title: "时间", dataIndex: "created_at", key: "time", width: 170,
      render: (t: string) => new Date(t).toLocaleString("zh-CN"),
    },
    {
      title: "操作人", dataIndex: "username", key: "username", width: 130,
    },
    {
      title: "操作", dataIndex: "action", key: "action", width: 170,
      render: (a: string) => (
        <Space>
          <Tag color="blue">{ACTION_LABEL[a] || a}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>{a}</Text>
        </Space>
      ),
    },
    {
      title: "对象", key: "resource", width: 160,
      render: (_, r) =>
        r.resource_type
          ? `${r.resource_type}${r.resource_id ? `(${r.resource_id.slice(0, 8)}…)` : ""}`
          : "-",
    },
    {
      title: "详情", dataIndex: "detail", key: "detail",
      render: (d: Record<string, unknown> | null) =>
        d ? (
          <Tooltip title={<pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(d, null, 2)}</pre>}>
            <Text ellipsis style={{ maxWidth: 320, display: "inline-block" }}>
              {JSON.stringify(d)}
            </Text>
          </Tooltip>
        ) : "-",
    },
    { title: "IP", dataIndex: "ip", key: "ip", width: 130, render: (v: string | null) => v || "-" },
  ];

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="审计日志" description="关键操作的完整留痕：谁在什么时候动了什么" />
      <Card>
        <Space style={{ marginBottom: 16 }} wrap>
          <Input
            placeholder="按操作人搜索"
            prefix={<SearchOutlined />}
            style={{ width: 200 }}
            allowClear
            onPressEnter={() => { setPage(1); fetchLogs(); }}
            onBlur={(e) => setUsernameFilter(e.target.value)}
          />
          <Select
            placeholder="按操作类型过滤"
            style={{ width: 220 }}
            allowClear
            value={actionFilter}
            onChange={setActionFilter}
            options={AUDIT_ACTION_OPTIONS}
            showSearch
            optionFilterProp="label"
          />
          <Select
            placeholder="按资源类型过滤"
            style={{ width: 180 }}
            allowClear
            value={resourceTypeFilter}
            onChange={setResourceTypeFilter}
            options={[
              { label: "知识库", value: "knowledge_base" },
              { label: "文档", value: "document" },
              { label: "分类", value: "category" },
              { label: "部门", value: "department" },
              { label: "用户", value: "user" },
              { label: "接入凭证", value: "mcp_credential" },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => { setPage(1); fetchLogs(); }}>
            查询
          </Button>
          <Button
            icon={<DownloadOutlined />}
            onClick={async () => {
              try {
                const blob = await adminApi.exportAuditLogs({
                  action: actionFilter,
                  username: usernameFilter || undefined,
                  resource_type: resourceTypeFilter,
                });
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = "audit_logs.csv";
                a.click();
                window.URL.revokeObjectURL(url);
                message.success("导出成功");
              } catch {
                message.error("导出失败");
              }
            }}
          >
            导出 CSV
          </Button>
        </Space>
        <Table
          columns={columns} dataSource={items} rowKey="id" loading={loading}
          pagination={{
            current: page, total, pageSize: 20, onChange: setPage,
            showTotal: (t) => `共 ${t} 条记录`,
          }}
        />
      </Card>
    </div>
  );
};

export default AuditLogPage;
