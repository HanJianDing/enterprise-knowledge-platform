/** 知识库与权限管理（企业知识库改造 阶段1）
 *
 * - 知识库列表：创建 / 改名 / 公共开关 / 删除（有文档时后端拒绝）
 * - 授权抽屉：按"部门 / 个人"两种主体授权，读 / 传 / 管三档权限
 *   判定规则：个人授权 > 部门授权 > 默认无权限；勾选"传/管"时"读"自动带上
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  Card, Table, Button, Space, Tag, Modal, Form, Input,
  InputNumber, Switch, Drawer, Select, Checkbox, Divider, Empty, Typography, Dropdown,
} from "antd";
import {
  PlusOutlined, EditOutlined, DeleteOutlined, TeamOutlined, ReloadOutlined,
  SlidersOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { kbApi } from "../../services/kbApi";
import { adminApi } from "../../services/adminApi";
import type { KnowledgeBaseInfo, KbMemberItem, KbMemberInput, DepartmentNode } from "../../types/kb";

const { Text } = Typography;

interface EditableGrant {
  key: string; // 本地行唯一键（未保存的行没有后端 id）
  subject_type: "dept" | "user";
  subject_id: string | undefined;
  can_read: boolean;
  can_upload: boolean;
  can_manage: boolean;
}

/** 已存在的授权（后端行）与本地新增行统一转成可编辑行 */
const toEditable = (m: KbMemberItem): EditableGrant => ({
  key: m.id,
  subject_type: m.subject_type,
  subject_id: m.subject_id,
  can_read: m.can_read,
  can_upload: m.can_upload,
  can_manage: m.can_manage,
});

const KnowledgeBaseManagePage: React.FC = () => {
  const [kbs, setKbs] = useState<KnowledgeBaseInfo[]>([]);
  const [loading, setLoading] = useState(false);

  const [kbModalOpen, setKbModalOpen] = useState(false);
  const [editingKb, setEditingKb] = useState<KnowledgeBaseInfo | null>(null);
  const [kbForm] = Form.useForm();

  // 授权抽屉
  const [grantKb, setGrantKb] = useState<KnowledgeBaseInfo | null>(null);
  const [grants, setGrants] = useState<EditableGrant[]>([]);
  const [depts, setDepts] = useState<DepartmentNode[]>([]);
  const [users, setUsers] = useState<{ id: string; username: string }[]>([]);
  const [savingGrants, setSavingGrants] = useState(false);

  // 库级检索配置（验收批次1·1d）
  const [retrievalKb, setRetrievalKb] = useState<KnowledgeBaseInfo | null>(null);
  const [retrievalForm] = Form.useForm<{ top_k: number | null; pagerank_alpha: number | null }>();
  const [savingRetrieval, setSavingRetrieval] = useState(false);

  const openRetrievalModal = async (kb: KnowledgeBaseInfo) => {
    setRetrievalKb(kb);
    try {
      const cfg = await adminApi.getKbRetrievalConfig(kb.id);
      retrievalForm.setFieldsValue({
        top_k: cfg.top_k ?? null,
        pagerank_alpha: cfg.pagerank_alpha ?? null,
      });
    } catch {
      message.error("加载检索配置失败");
    }
  };

  const handleSaveRetrieval = async () => {
    const values = await retrievalForm.validateFields();
    setSavingRetrieval(true);
    try {
      await adminApi.saveKbRetrievalConfig(retrievalKb!.id, values);
      message.success("库级检索配置已保存");
      setRetrievalKb(null);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "保存失败");
    } finally {
      setSavingRetrieval(false);
    }
  };

  const fetchKbs = async () => {
    setLoading(true);
    try {
      setKbs(await kbApi.listKbs());
    } catch {
      message.error("加载知识库列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchKbs(); }, []);

  /** 部门树 → Select 选项（带缩进） */
  const deptOptions = useMemo(() => {
    const opts: { label: string; value: string }[] = [];
    const walk = (nodes: DepartmentNode[], depth: number) => {
      for (const n of nodes) {
        opts.push({ label: `${"　".repeat(depth)}${n.name}`, value: n.id });
        if (n.children?.length) walk(n.children, depth + 1);
      }
    };
    walk(depts, 0);
    return opts;
  }, [depts]);

  const openGrantDrawer = async (kb: KnowledgeBaseInfo) => {
    setGrantKb(kb);
    try {
      const [members, deptList] = await Promise.all([
        kbApi.getMembers(kb.id),
        kbApi.getDepartments(),
      ]);
      setGrants(members.map(toEditable));
      setDepts(deptList);
      setUsers([]);
    } catch {
      message.error("加载授权信息失败");
    }
  };

  /** 远程搜索用户（授权抽屉选人，防 100 人截断——质量审查 LOW 修复） */
  const searchUsers = async (keyword: string) => {
    if (!keyword.trim()) {
      setUsers([]);
      return;
    }
    try {
      const r = await adminApi.getUsers(1, 50, keyword.trim());
      setUsers(r.items.map((u) => ({ id: u.id, username: u.username })));
    } catch {
      message.error("搜索用户失败");
    }
  };

  const saveGrants = async () => {
    if (!grantKb) return;
    // 校验：每行必须选了主体
    const incomplete = grants.find((g) => !g.subject_id);
    if (incomplete) {
      message.warning("有未选择主体（部门/用户）的授权行，请补全或删除该行");
      return;
    }
    // 校验：同一主体重复授权
    const seen = new Set<string>();
    for (const g of grants) {
      const k = `${g.subject_type}:${g.subject_id}`;
      if (seen.has(k)) {
        message.warning("同一部门/用户只能授权一次，请删除重复行");
        return;
      }
      seen.add(k);
    }
    const payload: KbMemberInput[] = grants.map((g) => ({
      subject_type: g.subject_type,
      subject_id: g.subject_id!,
      can_read: g.can_read,
      can_upload: g.can_upload,
      can_manage: g.can_manage,
    }));
    setSavingGrants(true);
    try {
      await kbApi.setMembers(grantKb.id, payload);
      message.success("授权已保存");
      setGrantKb(null);
      fetchKbs();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "保存失败");
    } finally {
      setSavingGrants(false);
    }
  };

  const updateGrant = (key: string, patch: Partial<EditableGrant>) => {
    setGrants((list) =>
      list.map((g) => {
        if (g.key !== key) return g;
        const next = { ...g, ...patch };
        // 能传/能管 → 读自动勾上；去掉传/管不动读
        if (next.can_upload || next.can_manage) next.can_read = true;
        return next;
      })
    );
  };

  const openCreateModal = () => {
    setEditingKb(null);
    kbForm.resetFields();
    kbForm.setFieldsValue({ is_public: false });
    setKbModalOpen(true);
  };

  const openEditModal = (kb: KnowledgeBaseInfo) => {
    setEditingKb(kb);
    kbForm.setFieldsValue({
      name: kb.name, description: kb.description, is_public: kb.is_public,
    });
    setKbModalOpen(true);
  };

  const handleSaveKb = async () => {
    try {
      const values = await kbForm.validateFields();
      if (editingKb) {
        await kbApi.updateKb(editingKb.id, values);
        message.success("知识库已更新");
      } else {
        await kbApi.createKb(values);
        message.success("知识库已创建");
      }
      setKbModalOpen(false);
      fetchKbs();
    } catch (err: any) {
      // 质量审查 LOW 修复：表单校验失败静默返回；API 错误（如同名库 400）要提示
      if (err?.response) {
        message.error(err.response.data?.detail || "保存失败");
      }
    }
  };

  const handleDeleteKb = async (kb: KnowledgeBaseInfo) => {
    try {
      await kbApi.deleteKb(kb.id);
      message.success("知识库已删除");
      fetchKbs();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "删除失败");
    }
  };

  const columns: ColumnsType<KnowledgeBaseInfo> = [
    { title: "知识库名称", dataIndex: "name", key: "name", width: 180, ellipsis: true },
    {
      title: "描述", dataIndex: "description", key: "desc", width: 240,
      render: (d: string | null) => <Typography.Paragraph style={{ margin: 0 }} ellipsis={{ rows: 2, tooltip: d }}>{d || "暂无描述"}</Typography.Paragraph>,
    },
    {
      title: "可见范围", dataIndex: "is_public", key: "public", width: 110,
      render: (pub: boolean) =>
        pub ? <Tag color="blue">全员可读</Tag> : <Tag color="orange">按授权访问</Tag>,
    },
    {
      title: "文档数", dataIndex: "document_count", key: "docs", width: 90,
    },
    {
      title: "授权数", dataIndex: "member_count", key: "members", width: 90,
    },
    {
      title: "创建时间", dataIndex: "created_at", key: "time", width: 160,
      render: (t: string) => new Date(t).toLocaleString("zh-CN"),
    },
    {
      title: "操作", key: "action", width: 160, fixed: "right",
      render: (_, record) => (
        <Space>
          <Button
            type="link" size="small" icon={<TeamOutlined />}
            onClick={() => openGrantDrawer(record)}
          >
            权限
          </Button>
          <Dropdown trigger={["click"]} menu={{ items: [
            { key: "edit", label: "编辑信息", icon: <EditOutlined />, onClick: () => openEditModal(record) },
            { key: "retrieval", label: "检索配置", icon: <SlidersOutlined />, onClick: () => openRetrievalModal(record) },
            { key: "delete", label: "删除知识库", icon: <DeleteOutlined />, danger: true, onClick: () => Modal.confirm({
              title: "确定删除此知识库？", content: "库内有文档时无法删除，请先核对知识库内容。", okText: "删除", cancelText: "取消",
              okButtonProps: { danger: true }, onOk: () => handleDeleteKb(record),
            }) },
          ] }}><Button size="small" type="text" aria-label={`更多操作：${record.name}`}>更多</Button></Dropdown>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-shell management-page">
      <PageHeader
        title="知识库与权限"
        description="创建知识库并分配访问权限：谁能看、谁能传文档、谁能管理"
      />
      <Card
        title="知识库列表"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchKbs}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
              新建知识库
            </Button>
          </Space>
        }
      >
        <Table columns={columns} dataSource={kbs} rowKey="id" loading={loading} pagination={false} scroll={{ x: 1030 }} tableLayout="fixed" />
      </Card>

      {/* 知识库创建/编辑 Modal */}
      <Modal
        title={editingKb ? "编辑知识库" : "新建知识库"}
        open={kbModalOpen}
        onOk={handleSaveKb}
        onCancel={() => setKbModalOpen(false)}
        okText="保存" cancelText="取消"
      >
        <Form form={kbForm} layout="vertical">
          <Form.Item name="name" label="知识库名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="例如: 人事制度库、财务制度库" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="可选，说明这个库里放什么内容" rows={2} />
          </Form.Item>
          <Form.Item name="is_public" label="全员可读（公共库）" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Text type="secondary">
            公共库 = 所有员工都能检索；私有库 = 只有被授权的人能看（在「权限」中分配）
          </Text>
        </Form>
      </Modal>

      {/* 授权抽屉 */}
      <Drawer
        title={grantKb ? `「${grantKb.name}」的访问权限` : ""}
        open={!!grantKb}
        onClose={() => setGrantKb(null)}
        width={720}
        extra={
          <Button type="primary" loading={savingGrants} onClick={saveGrants}>
            保存授权
          </Button>
        }
      >
        <Text type="secondary">
          规则：个人授权 &gt; 部门授权 &gt; 默认无权限（管理员不受限）；勾选"传文档/管理"会自动勾上"查看"。
        </Text>
        <Divider />
        {grants.length === 0 ? (
          <Empty description="还没有授权，点击下方按钮添加" />
        ) : (
          grants.map((g) => (
            <Card key={g.key} size="small" style={{ marginBottom: 12 }}>
              <Space wrap>
                <Select
                  style={{ width: 110 }}
                  value={g.subject_type}
                  onChange={(v) => updateGrant(g.key, { subject_type: v, subject_id: undefined })}
                  options={[
                    { label: "部门", value: "dept" },
                    { label: "个人", value: "user" },
                  ]}
                />
                {g.subject_type === "dept" ? (
                  <Select
                    style={{ width: 200 }}
                    showSearch
                    optionFilterProp="label"
                    placeholder="选择部门"
                    value={g.subject_id}
                    onChange={(v) => updateGrant(g.key, { subject_id: v })}
                    options={deptOptions}
                  />
                ) : (
                  <Select
                    style={{ width: 200 }}
                    showSearch
                    filterOption={false}
                    onSearch={searchUsers}
                    placeholder="输入用户名搜索"
                    value={g.subject_id}
                    onChange={(v) => updateGrant(g.key, { subject_id: v })}
                    options={users.map((u) => ({ label: u.username, value: u.id }))}
                    notFoundContent={null}
                  />
                )}
                <Checkbox
                  checked={g.can_read}
                  onChange={(e) => updateGrant(g.key, { can_read: e.target.checked })}
                >
                  查看
                </Checkbox>
                <Checkbox
                  checked={g.can_upload}
                  onChange={(e) => updateGrant(g.key, { can_upload: e.target.checked })}
                >
                  传文档
                </Checkbox>
                <Checkbox
                  checked={g.can_manage}
                  onChange={(e) => updateGrant(g.key, { can_manage: e.target.checked })}
                >
                  管理
                </Checkbox>
                <Button
                  type="text" danger size="small" icon={<DeleteOutlined />}
                  onClick={() => setGrants((list) => list.filter((x) => x.key !== g.key))}
                >
                  移除
                </Button>
              </Space>
            </Card>
          ))
        )}
        <Button
          block icon={<PlusOutlined />}
          onClick={() =>
            setGrants((list) => [
              ...list,
              {
                key: `new-${Date.now()}-${list.length}`,
                subject_type: "dept",
                subject_id: undefined,
                can_read: true,
                can_upload: false,
                can_manage: false,
              },
            ])
          }
        >
          添加授权
        </Button>
      </Drawer>
      <Modal
        title={`检索配置（${retrievalKb?.name ?? ""}）`}
        open={retrievalKb !== null}
        onCancel={() => setRetrievalKb(null)}
        onOk={handleSaveRetrieval}
        confirmLoading={savingRetrieval}
        okText="保存"
      >
        <div style={{ color: "#999", fontSize: 13, marginBottom: 12 }}>
          留空 = 使用全局默认（调参不改代码）；仅对该库生效，保存后即时生效。
        </div>
        <Form form={retrievalForm} layout="horizontal" labelCol={{ span: 8 }}>
          <Form.Item name="top_k" label="候选数 top_k" extra="默认 20（rerank 粗召回窗口）">
            <InputNumber min={1} max={50} style={{ width: 160 }} placeholder="全局默认" />
          </Form.Item>
          <Form.Item name="pagerank_alpha" label="pagerank 加成 α" extra="0=关闭；建议 0~0.3">
            <InputNumber min={0} max={1} step={0.05} style={{ width: 160 }} placeholder="全局默认" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default KnowledgeBaseManagePage;
