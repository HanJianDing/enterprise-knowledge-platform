/** MCP 工具管理页（批次F 挂清单 #13 页面化）
 *
 * 外部 MCP Server 配置管理——此前只有 .env 手改+重启；此页支持
 * 新增（http/stdio 双传输）、测试连接（列出工具）、启停、删除，
 * 配置落库即时生效（问答链路每问开头刷新配置缓存）。
 */
import React, { useEffect, useState } from "react";
import {
  Button, Card, Input, Modal, Popconfirm, Select, Switch, Table, Tag, Typography, Alert,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi, type McpServerInput, type McpServerItem } from "../../services/adminApi";

const { Text } = Typography;

const MCPToolsPage: React.FC = () => {
  const [items, setItems] = useState<McpServerItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    name: "",
    transport: "http" as "http" | "stdio",
    url: "",
    command: "",
    argsText: "",
  });
  const [testResult, setTestResult] = useState<
    { ok: boolean; tools: { name: string; description: string }[]; error?: string } | null
  >(null);

  const fetchList = async () => {
    setLoading(true);
    try {
      setItems(await adminApi.mcpServerList());
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "加载 MCP 工具配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchList(); }, []);

  const buildInput = (): McpServerInput | null => {
    if (!form.name.trim()) {
      message.warning("请填写服务名");
      return null;
    }
    if (form.transport === "http") {
      if (!form.url.trim()) {
        message.warning("请填写服务地址 URL");
        return null;
      }
      return { name: form.name.trim(), transport: "http", url: form.url.trim() };
    }
    if (!form.command.trim()) {
      message.warning("请填写启动命令");
      return null;
    }
    const args = form.argsText.trim()
      ? form.argsText.trim().split(/\s+/).filter(Boolean)
      : [];
    return { name: form.name.trim(), transport: "stdio", command: form.command.trim(), args };
  };

  const handleCreate = async () => {
    const input = buildInput();
    if (!input) return;
    setCreating(true);
    try {
      await adminApi.mcpServerCreate(input);
      message.success("已添加 MCP 工具服务");
      setCreateOpen(false);
      setForm({ name: "", transport: "http", url: "", command: "", argsText: "" });
      fetchList();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "添加失败");
    } finally {
      setCreating(false);
    }
  };

  const handleToggle = async (row: McpServerItem, enabled: boolean) => {
    try {
      await adminApi.mcpServerToggle(row.id, enabled);
      message.success(enabled ? "已启用" : "已停用");
      fetchList();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "操作失败");
    }
  };

  const handleDelete = async (row: McpServerItem) => {
    try {
      await adminApi.mcpServerDelete(row.id);
      message.success("已删除");
      fetchList();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "删除失败");
    }
  };

  const handleTest = async (row: McpServerItem) => {
    setTestResult(null);
    const input: McpServerInput = row.transport === "http"
      ? { name: row.name, transport: "http", url: row.url || "" }
      : { name: row.name, transport: "stdio", command: row.command || "", args: row.args };
    try {
      const r = await adminApi.mcpServerTest(input);
      setTestResult(r);
    } catch (err: any) {
      setTestResult({ ok: false, tools: [], error: err?.response?.data?.detail || "测试失败" });
    }
  };

  const columns: ColumnsType<McpServerItem> = [
    { title: "服务名", dataIndex: "name", key: "name", width: 140 },
    { title: "传输方式", dataIndex: "transport", key: "transport", width: 100,
      render: (t: string) => <Tag color={t === "stdio" ? "purple" : "cyan"}>{t}</Tag> },
    { title: "地址/命令", key: "addr",
      render: (_, r) => r.transport === "http"
        ? <Text code>{r.url}</Text>
        : <Text code>{r.command}{r.args.length > 0 ? ` ${r.args.join(" ")}` : ""}</Text> },
    { title: "状态", dataIndex: "enabled", key: "enabled", width: 90,
      render: (enabled: boolean, r) => (
        <Switch
          checked={enabled}
          checkedChildren="启用"
          unCheckedChildren="停用"
          onChange={(v) => handleToggle(r, v)}
        />
      ) },
    { title: "创建时间", dataIndex: "created_at", key: "created", width: 180,
      render: (t: string) => new Date(t).toLocaleString() },
    { title: "操作", key: "op", width: 170,
      render: (_, r) => (
        <>
          <Button type="link" size="small" onClick={() => handleTest(r)}>测试连接</Button>
          <Popconfirm title="删除此配置？" description="删除后问答不再使用该服务"
            okText="删除" cancelText="取消" onConfirm={() => handleDelete(r)}>
            <Button type="link" size="small" danger>删除</Button>
          </Popconfirm>
        </>
      ) },
  ];

  return (
    <div>
      <PageHeader
        title="MCP 工具"
        description="外部 MCP Server 接入（如 OA 系统查报销进度）——配置落库即时生效，问答按需调用"
      />
      <Card>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="支持两种接入：http（远程流式端点）与 stdio（本地进程，如 npx 启动的 MCP Server）。停用/删除后即时从问答工具候选移除。注：服务器 .env 的 MCP_SERVERS 兜底配置不在此页管理，同名项问答时仍会生效。"
        />
        <Card
          size="small"
          title="工具服务列表"
          style={{ marginBottom: 16 }}
          extra={(
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              添加服务
            </Button>
          )}
        >
          <Table
            columns={columns}
            dataSource={items}
            rowKey={(r) => r.id}
            loading={loading}
            pagination={false}
            size="small"
          />
        </Card>

        {testResult && (
          <Alert
            type={testResult.ok ? "success" : "error"}
            showIcon
            closable
            onClose={() => setTestResult(null)}
            message={testResult.ok
              ? `连接成功，可用工具 ${testResult.tools.length} 个`
              : `连接失败：${testResult.error || "未知错误"}`}
            description={testResult.ok && testResult.tools.length > 0 ? (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {testResult.tools.map((t) => (
                  <li key={t.name}><b>{t.name}</b>{t.description ? `：${t.description}` : ""}</li>
                ))}
              </ul>
            ) : undefined}
          />
        )}
      </Card>

      <Modal
        title="添加 MCP 工具服务"
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false);
          setForm({ name: "", transport: "http", url: "", command: "", argsText: "" });
        }}
        onOk={handleCreate}
        okText="添加"
        cancelText="取消"
        confirmLoading={creating}
      >
        <Input
          placeholder="服务名（工具名前缀，如 oa）"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          style={{ marginBottom: 12 }}
        />
        <Select
          value={form.transport}
          onChange={(v) => setForm({ ...form, transport: v })}
          style={{ width: "100%", marginBottom: 12 }}
          options={[
            { label: "http（远程流式端点）", value: "http" },
            { label: "stdio（本地进程）", value: "stdio" },
          ]}
        />
        {form.transport === "http" ? (
          <Input
            placeholder="服务地址，如 http://oa.internal/mcp/"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
            style={{ marginBottom: 12 }}
          />
        ) : (
          <>
            <Input
              placeholder="启动命令，如 npx"
              value={form.command}
              onChange={(e) => setForm({ ...form, command: e.target.value })}
              style={{ marginBottom: 12 }}
            />
            <Input
              placeholder="参数（空格分隔），如 -y @modelcontextprotocol/server-everything"
              value={form.argsText}
              onChange={(e) => setForm({ ...form, argsText: e.target.value })}
            />
          </>
        )}
      </Modal>
    </div>
  );
};

export default MCPToolsPage;
