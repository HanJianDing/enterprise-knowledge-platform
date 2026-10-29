import React, { useEffect, useState } from "react";
import {
  Card, Table, Button, Tag, Popconfirm, Select, Modal, Space, Typography,
  Form, Input, Upload,
} from "antd";
import { ApartmentOutlined, PlusOutlined, UploadOutlined, KeyOutlined } from "@ant-design/icons";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import type { ColumnsType } from "antd/es/table";
import { adminApi } from "../../services/adminApi";
import { kbApi } from "../../services/kbApi";
import type { UserInfo, UserRole } from "../../types/user";
import type { DepartmentNode } from "../../types/kb";

const { Text } = Typography;

const ROLE_LABEL: Record<UserRole, string> = {
  admin: "管理员",
  editor: "编辑",
  user: "员工",
  auditor: "审计员",
};

const UserManagementPage: React.FC = () => {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);

  // 所属部门设置（企业知识库阶段1：员工挂部门后自动继承部门对知识库的授权）
  const [deptModalUser, setDeptModalUser] = useState<UserInfo | null>(null);
  // 重置密码（产品化 C2：重置后该账号首登强制改密 + 旧会话被踢）
  const [resetUser, setResetUser] = useState<UserInfo | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetForm] = Form.useForm();

  const handleResetPassword = async () => {
    try {
      const values = await resetForm.validateFields();
      if (!resetUser) return;
      setResetting(true);
      const r = await adminApi.resetPassword(resetUser.id, values.new_password);
      message.success(r.message);
      setResetOpen(false);
      resetForm.resetFields();
    } catch (err: any) {
      if (err?.response?.data?.detail) message.error(err.response.data.detail);
    } finally {
      setResetting(false);
    }
  };
  const [depts, setDepts] = useState<DepartmentNode[]>([]);
  const [checkedDeptIds, setCheckedDeptIds] = useState<string[]>([]);
  const [savingDepts, setSavingDepts] = useState(false);

  // 新建员工（企业知识库阶段3：开放注册已关闭，建号走这里）
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [creating, setCreating] = useState(false);

  const fetchUsers = async () => {
    setLoading(true);
    try {
      const r = await adminApi.getUsers(page);
      setUsers(r.items);
      setTotal(r.total);
    } catch {
      message.error("加载用户列表失败");
    } finally {
      setLoading(false);
    }
  };

  const fetchDepts = async () => {
    try {
      setDepts(await kbApi.getDepartments());
    } catch {
      message.error("加载部门列表失败");
    }
  };

  useEffect(() => { fetchUsers(); }, [page]);

  const handleChangeRole = async (userId: string, role: UserRole) => {
    try {
      await adminApi.changeRole(userId, role);
      message.success("角色已更新");
      fetchUsers();
    } catch { message.error("操作失败"); }
  };

  const handleToggleStatus = async (userId: string, isActive: boolean) => {
    try {
      await adminApi.changeStatus(userId, isActive);
      message.success(isActive ? "用户已启用" : "用户已禁用");
      fetchUsers();
    } catch { message.error("操作失败"); }
  };

  const openDeptModal = async (user: UserInfo) => {
    setDeptModalUser(user);
    await fetchDepts();
    try {
      const owned = await kbApi.getUserDepartments(user.id);
      setCheckedDeptIds(owned.map((d) => d.id));
    } catch {
      message.error("加载部门信息失败");
    }
  };

  const saveUserDepts = async () => {
    if (!deptModalUser) return;
    setSavingDepts(true);
    try {
      await kbApi.setUserDepartments(deptModalUser.id, checkedDeptIds);
      message.success("所属部门已更新");
      setDeptModalUser(null);
    } catch (err: any) {
      message.error(err.response?.data?.detail || "保存失败");
    } finally {
      setSavingDepts(false);
    }
  };

  const openCreateModal = async () => {
    createForm.resetFields();
    createForm.setFieldsValue({ role: "user" });
    setCreateOpen(true);
    await fetchDepts();
  };

  const handleCreateUser = async () => {
    try {
      const values = await createForm.validateFields();
      setCreating(true);
      await adminApi.createUser({
        username: values.username,
        email: values.email,
        password: values.password,
        role: values.role,
        department_ids: values.department_ids || [],
      });
      message.success("账号已创建，员工首次登录需修改初始密码");
      setCreateOpen(false);
      fetchUsers();
    } catch (err: any) {
      if (err?.response) {
        message.error(err.response.data?.detail || "创建失败");
      }
    } finally {
      setCreating(false);
    }
  };

  const handleImportCsv = async (file: File) => {
    try {
      const result = await adminApi.importUsers(file);
      if (result.errors.length) {
        message.warning(
          `导入完成：成功 ${result.created} 人，失败 ${result.errors.length} 行` +
          `（第 ${result.errors.map((e) => e.row).join("、")} 行）`,
          8
        );
      } else {
        message.success(`导入成功：${result.created} 人`);
      }
      fetchUsers();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "导入失败");
    }
    return false;
  };

  const deptOptions = (nodes: DepartmentNode[], depth = 0): { label: string; value: string }[] =>
    nodes.flatMap((n) => [
      { label: `${"　".repeat(depth)}${n.name}`, value: n.id },
      ...deptOptions(n.children || [], depth + 1),
    ]);

  const columns: ColumnsType<UserInfo> = [
    { title: "用户名", dataIndex: "username", key: "username" },
    { title: "邮箱", dataIndex: "email", key: "email" },
    {
      title: "角色", dataIndex: "role", key: "role", width: 140,
      render: (role: UserRole, record) => (
        <Select
          value={role}
          style={{ width: 100 }}
          onChange={(v) => handleChangeRole(record.id, v as UserRole)}
          options={(Object.keys(ROLE_LABEL) as UserRole[]).map((r) => ({
            label: ROLE_LABEL[r], value: r,
          }))}
        />
      ),
    },
    {
      title: "状态", dataIndex: "is_active", key: "status", width: 100,
      render: (active: boolean) => active ? <Tag color="green">正常</Tag> : <Tag color="red">禁用</Tag>,
    },
    {
      title: "注册时间", dataIndex: "created_at", key: "time", width: 160,
      render: (t: string) => t ? new Date(t).toLocaleString("zh-CN") : "-",
    },
    {
      title: "操作", key: "action", width: 200,
      render: (_, record) => (
        <Space>
          <Button
            type="link" size="small" icon={<ApartmentOutlined />}
            onClick={() => openDeptModal(record)}
          >
            所属部门
          </Button>
          <Button
            type="link" size="small" icon={<KeyOutlined />}
            onClick={() => { setResetUser(record); setResetOpen(true); }}
          >
            重置密码
          </Button>
          <Popconfirm
            title={`确定${record.is_active ? "禁用" : "启用"}此用户?`}
            onConfirm={() => handleToggleStatus(record.id, !record.is_active)}
          >
            <Button type="link" danger={record.is_active}>
              {record.is_active ? "禁用" : "启用"}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="用户管理" description="角色、状态与所属部门管理；员工账号由管理员创建或 CSV 批量导入" />
      <Card
        title="用户列表"
        extra={
          <Space>
            <Upload
              beforeUpload={handleImportCsv}
              showUploadList={false}
              accept=".csv"
            >
              <Button icon={<UploadOutlined />}>CSV 批量导入</Button>
            </Upload>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
              新建员工
            </Button>
          </Space>
        }
      >
        <Table
          columns={columns} dataSource={users} rowKey="id" loading={loading}
          pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
        />
      </Card>

      {/* 重置密码 Modal（产品化 C2） */}
      <Modal
        title={resetUser ? `重置「${resetUser.username}」的密码` : ""}
        open={resetOpen}
        onOk={handleResetPassword}
        onCancel={() => { setResetOpen(false); resetForm.resetFields(); }}
        okText="重置" cancelText="取消" confirmLoading={resetting}
      >
        <Form form={resetForm} layout="vertical">
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: "请输入新密码" },
              { min: 8, message: "至少 8 位" },
              { pattern: /^(?=.*[A-Za-z])(?=.*\d).+$/, message: "需同时包含字母和数字" },
            ]}
          >
            <Input.Password placeholder="至少 8 位，含字母和数字" />
          </Form.Item>
        </Form>
        <Text type="secondary" style={{ fontSize: 12 }}>
          重置后该账号首次登录将被要求修改密码，已登录的会话会被踢下线。
        </Text>
      </Modal>

      {/* 所属部门 Modal */}
      <Modal
        title={deptModalUser ? `「${deptModalUser.username}」的所属部门` : ""}
        open={!!deptModalUser}
        onOk={saveUserDepts}
        onCancel={() => setDeptModalUser(null)}
        okText="保存" cancelText="取消" confirmLoading={savingDepts}
      >
        <Select
          mode="multiple"
          style={{ width: "100%" }}
          placeholder="选择部门（可多选，不选则不属于任何部门）"
          value={checkedDeptIds}
          onChange={setCheckedDeptIds}
          options={deptOptions(depts)}
          optionFilterProp="label"
        />
        <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
          员工属于哪个部门，就自动获得该部门被授权的知识库访问权限
        </Text>
      </Modal>

      {/* 新建员工 Modal */}
      <Modal
        title="新建员工账号"
        open={createOpen}
        onOk={handleCreateUser}
        onCancel={() => setCreateOpen(false)}
        okText="创建" cancelText="取消" confirmLoading={creating}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, min: 2, message: "请输入用户名（至少2位）" }]}>
            <Input placeholder="例如: zhangsan" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请输入有效邮箱" }]}>
            <Input placeholder="例如: zhangsan@company.com" />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: "请输入初始密码" },
              { min: 8, message: "至少 8 位" },
              { pattern: /^(?=.*[A-Za-z])(?=.*\d).+$/, message: "需同时包含字母和数字" },
            ]}
          >
            <Input.Password placeholder="至少 8 位，含字母和数字；员工首次登录需修改" />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="user">
            <Select options={(Object.keys(ROLE_LABEL) as UserRole[]).map((r) => ({
              label: ROLE_LABEL[r], value: r,
            }))} />
          </Form.Item>
          <Form.Item name="department_ids" label="所属部门">
            <Select
              mode="multiple"
              allowClear
              placeholder="可多选"
              options={deptOptions(depts)}
              optionFilterProp="label"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default UserManagementPage;
