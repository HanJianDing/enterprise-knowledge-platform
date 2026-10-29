import React, { useEffect, useState } from "react";
import {
  Button, Card, DatePicker, Input, Modal, Popconfirm, Select, Table, Tag, Typography,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi, type ApiKeyItem } from "../../services/adminApi";
import { kbApi } from "../../services/kbApi";
import type { KnowledgeBaseInfo } from "../../types/kb";

const { Paragraph, Text } = Typography;

/** API Key 管理页（批次11e OpenAI 兼容 API）：
 * 生成 key（明文仅一次展示）→ 通用 LLM 客户端以 Bearer key 调 /v1/chat/completions
 * 批次A·11e backlog：expires_at 管理入口——生成/修改可设过期时间，列表展示 */
const ApiKeyPage: React.FC = () => {
  const [keys, setKeys] = useState<ApiKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createExpires, setCreateExpires] = useState<string | null>(null);
  // 批次F 挂清单 #12：库白名单（空=穿透账号权限）
  const [createKbIds, setCreateKbIds] = useState<string[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBaseInfo[]>([]);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [expiryKey, setExpiryKey] = useState<ApiKeyItem | null>(null);
  const [expiryValue, setExpiryValue] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setKeys(await adminApi.apiKeyList());
    } catch (err: any) {
      message.error(err.response?.data?.detail || "加载失败");
    } finally {
      setLoading(false);
    }
  };

  // 库列表：页面加载即取（#12 白名单选项 + 列表"库范围"列回显库名共用）
  useEffect(() => {
    load();
    kbApi.listKbs().then(setKbs).catch(() => { /* 静默 */ });
  }, []);

  // 挂清单 #11 修复：列表 60s 自动轮询——key 设短期过期后到点不
  // 自动变"已过期"（真实验收：需手动刷新网页）。质检 P3-2：轮询
  // 静默刷新（不 setLoading，避免每分钟闪一次 spinner）
  useEffect(() => {
    const timer = setInterval(() => {
      adminApi.apiKeyList().then(setKeys).catch(() => { /* 静默 */ });
    }, 60_000);
    return () => clearInterval(timer);
  }, []);

  const handleCreate = async () => {
    if (!createName.trim()) {
      message.warning("请填写用途备注");
      return;
    }
    try {
      const r = await adminApi.createApiKey(
        createName.trim(), createExpires,
        createKbIds.length > 0 ? createKbIds : null,
      );
      setNewKey(r.key);
      setCreateOpen(false);
      setCreateName("");
      setCreateExpires(null);
      setCreateKbIds([]);
      await load();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "生成失败");
    }
  };

  const handleRevoke = async (keyId: string) => {
    try {
      await adminApi.revokeApiKey(keyId);
      message.success("已吊销");
      await load();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "吊销失败");
    }
  };

  const handleSetExpiry = async () => {
    if (!expiryKey) return;
    // 批次B.5 P3-7：过去时间会 400（后端拒绝）——前端先行拦截给明确提示
    if (expiryValue && new Date(expiryValue).getTime() < Date.now()) {
      message.warning("过期时间必须是未来时间");
      return;
    }
    try {
      await adminApi.setApiKeyExpiry(expiryKey.id, expiryValue);
      message.success(expiryValue ? "已设置过期时间" : "已取消过期时间");
      setExpiryKey(null);
      setExpiryValue(null);
      await load();
    } catch (err) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })
          ?.response?.data?.detail || "设置失败",
      );
    }
  };

  const renderExpiry = (t: string | null) => {
    if (!t) return <Text type="secondary">永不过期</Text>;
    const expired = new Date(t).getTime() < Date.now();
    return expired
      ? <Tag color="red">已过期 {new Date(t).toLocaleDateString()}</Tag>
      : <Text>{new Date(t).toLocaleString()}</Text>;
  };

  const columns: ColumnsType<ApiKeyItem> = [
    { title: "用途", dataIndex: "name", key: "name" },
    { title: "库范围", dataIndex: "kb_ids", key: "kb", width: 140,
      // 批次F 挂清单 #12：白名单回显——null=全部可见库（穿透账号权限）
      render: (ids: string[] | null) => {
        if (!ids || ids.length === 0) {
          return <Text type="secondary">全部可见库</Text>;
        }
        const names = ids
          .map((id) => kbs.find((k) => k.id === id)?.name || id.slice(0, 8))
          .join("、");
        return <Tag color="geekblue">{names}</Tag>;
      } },
    { title: "Key 前缀", dataIndex: "prefix", key: "prefix", width: 180,
      render: (p: string) => <Text code>{p}…</Text> },
    { title: "状态", dataIndex: "is_active", key: "active", width: 90,
      // 挂清单 #10 修复：状态列综合渲染——过期优先标"已过期"，其次
      // 吊销（此前 is_active 与 expires_at 独立，已过期 key 却标
      // "有效"，管理员困惑"已过期但有效"）
      render: (a: boolean, r) => {
        if (r.expires_at && new Date(r.expires_at).getTime() < Date.now()) {
          return <Tag color="red">已过期</Tag>;
        }
        return a
          ? <Tag color="green">有效</Tag>
          : <Tag color="default">已吊销</Tag>;
      } },
    { title: "过期时间", dataIndex: "expires_at", key: "expires", width: 180,
      render: renderExpiry },
    { title: "最近使用", dataIndex: "last_used_at", key: "used", width: 180,
      render: (t: string | null) => t ? new Date(t).toLocaleString() : "—" },
    { title: "创建时间", dataIndex: "created_at", key: "created", width: 180,
      render: (t: string) => new Date(t).toLocaleString() },
    {
      title: "操作", key: "op", width: 160,
      render: (_, r) => r.is_active ? (
        <>
          <Button type="link" size="small"
            onClick={() => {
              setExpiryKey(r);
              // 批次B.5 P3-7：已过期的旧值不预填（原样点确定会 400）
              setExpiryValue(
                r.expires_at && new Date(r.expires_at).getTime() > Date.now()
                  ? r.expires_at : null,
              );
            }}>
            设置过期
          </Button>
          <Popconfirm title="吊销此 key？" description="吊销后立即失效，不可恢复"
            okText="吊销" cancelText="取消" onConfirm={() => handleRevoke(r.id)}>
            <Button type="link" size="small" danger>吊销</Button>
          </Popconfirm>
        </>
      ) : null,
    },
  ];

  return (
    <div>
      <PageHeader
        title="API Key"
        description="OpenAI 兼容接口凭据——Dify / n8n / Coze / LangChain 等通用客户端以 Bearer key 调用 /v1/chat/completions，零学习成本接入知识库问答"
      />
      <Card
        title="接入方式"
        size="small"
        style={{ marginBottom: 16 }}
      >
        <Paragraph style={{ marginBottom: 0 }}>
          <Text code>POST /v1/chat/completions</Text>（OpenAI 风格路径，客户端
          base_url 直指部署域名；本地演示为 <Text code>http://localhost:8010/v1/chat/completions</Text>）<br />
          请求头：<Text code>Authorization: Bearer &lt;key&gt;</Text><br />
          请求体（OpenAI 协议）：<Text code>{'{"model":"rag-knowledge-base","messages":[{"role":"user","content":"年假怎么申请"}],"stream":false}'}</Text>
        </Paragraph>
      </Card>
      <Card
        title="Key 列表"
        extra={(
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            生成 Key
          </Button>
        )}
      >
        <Table
          columns={columns}
          dataSource={keys}
          rowKey={(r) => r.id}
          loading={loading}
          pagination={false}
          size="small"
        />
      </Card>

      <Modal
        title="生成 API Key"
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false); setCreateName(""); setCreateExpires(null);
          setCreateKbIds([]);
        }}
        onOk={handleCreate}
        okText="生成"
        cancelText="取消"
      >
        <Input
          placeholder="用途备注（如：Dify 工作流接入）"
          value={createName}
          onChange={(e) => setCreateName(e.target.value)}
          style={{ marginBottom: 12 }}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="库范围（可选，不选=全部可见库）"
          style={{ width: "100%", marginBottom: 12 }}
          value={createKbIds}
          onChange={setCreateKbIds}
          options={kbs.map((k) => ({ label: k.name, value: k.id }))}
        />
        <DatePicker
          showTime
          placeholder="过期时间（可选，不选=永不过期）"
          style={{ width: "100%" }}
          // 批次F 验收修复：antd 6 DatePicker value 必须传 dayjs——
          // 传原生 Date 触发 getUDayjs().isValid 崩溃（白屏）
          value={createExpires ? dayjs(createExpires) : null}
          onChange={(d) => setCreateExpires(d ? d.toISOString() : null)}
        />
      </Modal>

      <Modal
        title={`设置过期时间：${expiryKey?.name ?? ""}`}
        open={expiryKey !== null}
        onCancel={() => { setExpiryKey(null); setExpiryValue(null); }}
        onOk={handleSetExpiry}
        okText="保存"
        cancelText="取消"
      >
        <DatePicker
          showTime
          placeholder="选择过期时间"
          style={{ width: "100%" }}
          value={expiryValue ? dayjs(expiryValue) : null}
          onChange={(d) => setExpiryValue(d ? d.toISOString() : null)}
          disabledDate={(d) => d.toDate().getTime() < Date.now() - 86400000}
        />
        <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
          <Text type="secondary">清空选择并保存 = 取消过期（永不过期）。过期后接口返回 401。</Text>
        </Paragraph>
      </Modal>

      <Modal
        title="Key 生成成功（仅显示一次，请立即保存）"
        open={newKey !== null}
        onCancel={() => setNewKey(null)}
        footer={<Button type="primary" onClick={() => setNewKey(null)}>我已保存</Button>}
      >
        {/* 批次F 验收修复：copyable 显式传 text——children 是嵌套
            Text 元素时 antd 6 的 toText 转换会把元素 String 成
            "[object Object]"，复制出来不是 key（真实验收发现） */}
        <Paragraph copyable={{ text: newKey ?? "" }} style={{ wordBreak: "break-all" }}>
          <Text code>{newKey}</Text>
        </Paragraph>
        <Text type="warning">关闭后无法再次查看明文，遗失需重新生成。</Text>
      </Modal>
    </div>
  );
};

export default ApiKeyPage;
