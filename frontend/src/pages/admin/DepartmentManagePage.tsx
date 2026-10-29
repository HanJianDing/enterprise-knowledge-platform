/** 部门管理（企业知识库改造 阶段1）
 *
 * 部门树维护：创建 / 改名 / 调整上级 / 删除（有子部门时后端拒绝）。
 * 部门建好后：① 在「知识库与权限」页把库授权给部门；② 在「用户管理」页把员工挂到部门。
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  Card, Table, Button, Space, Popconfirm, Modal, Form, Input, TreeSelect,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { kbApi } from "../../services/kbApi";
import type { DepartmentNode } from "../../types/kb";

const DepartmentManagePage: React.FC = () => {
  const [depts, setDepts] = useState<DepartmentNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<DepartmentNode | null>(null);
  const [form] = Form.useForm();

  const fetchDepts = async () => {
    setLoading(true);
    try {
      setDepts(await kbApi.getDepartments());
    } catch {
      message.error("加载部门列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchDepts(); }, []);

  const treeData = useMemo(() => {
    const toTree = (nodes: DepartmentNode[]): any[] =>
      nodes.map((n) => ({
        title: n.name, value: n.id,
        children: n.children?.length ? toTree(n.children) : undefined,
      }));
    return toTree(depts);
  }, [depts]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (d: DepartmentNode) => {
    setEditing(d);
    form.setFieldsValue({ name: d.name, parent_id: d.parent_id ?? undefined });
    setModalOpen(true);
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      if (editing) {
        await kbApi.updateDepartment(editing.id, {
          name: values.name,
          parent_id: values.parent_id ?? null,
        });
        message.success("部门已更新");
      } else {
        await kbApi.createDepartment({ name: values.name, parent_id: values.parent_id ?? null });
        message.success("部门已创建");
      }
      setModalOpen(false);
      fetchDepts();
    } catch (err: any) {
      // 质量审查 LOW 修复：表单校验失败静默返回；API 错误（如重名 400）要提示
      if (err?.response) {
        message.error(err.response.data?.detail || "保存失败");
      }
    }
  };

  const handleDelete = async (d: DepartmentNode) => {
    try {
      await kbApi.deleteDepartment(d.id);
      message.success("部门已删除");
      fetchDepts();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "删除失败");
    }
  };

  const columns: ColumnsType<DepartmentNode> = [
    { title: "部门名称", dataIndex: "name", key: "name" },
    {
      title: "子部门数", key: "children", width: 100,
      render: (_, record) => record.children?.length || 0,
    },
    {
      title: "创建时间", dataIndex: "created_at", key: "time", width: 170,
      render: (t: string) => new Date(t).toLocaleString("zh-CN"),
    },
    {
      title: "操作", key: "action", width: 150,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确定删除此部门?"
            description="有子部门时会拒绝删除；删除后该部门的库授权与成员关系一并清理"
            onConfirm={() => handleDelete(record)}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <PageHeader
        title="部门管理"
        description="维护组织架构树；部门建好后可被授权访问知识库，员工挂到部门后自动继承权限"
      />
      <Card
        title="部门树"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchDepts}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新建部门
            </Button>
          </Space>
        }
      >
        <Table
          columns={columns} dataSource={depts} rowKey="id" loading={loading}
          pagination={false} expandable={{ childrenColumnName: "children", defaultExpandAllRows: true }}
        />
      </Card>

      <Modal
        title={editing ? "编辑部门" : "新建部门"}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        okText="保存" cancelText="取消"
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="部门名称" rules={[{ required: true, message: "请输入部门名称" }]}>
            <Input placeholder="例如: 人事部、财务部" />
          </Form.Item>
          <Form.Item name="parent_id" label="上级部门">
            <TreeSelect
              placeholder="无（顶级部门）"
              allowClear
              treeDefaultExpandAll
              treeData={
                editing
                  ? treeData.filter((n) => n.value !== editing.id)
                  : treeData
              }
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default DepartmentManagePage;
