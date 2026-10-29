import React, { useState } from "react";
import { Alert, Card, Form, Input, Button } from "antd";
import { message } from "../../utils/message";
import { LockOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { authApi } from "../../services/authApi";
import { useAuthStore } from "../../stores/authStore";

const ChangePasswordPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  const onFinish = async (values: {
    old_password: string;
    new_password: string;
  }) => {
    setLoading(true);
    try {
      await authApi.changePassword(values);
      // 改密后后端 sv+1 踢掉所有旧令牌（当前会话已失效）：
      // 直接清空登录态回登录页，用新密码重新登录（与后端"改密强制重登"设计对齐）
      message.success("密码修改成功，请用新密码重新登录");
      useAuthStore.getState().logout();
      navigate("/login");
    } catch (err: any) {
      message.error(err.response?.data?.detail || "修改失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card title="修改密码" style={{ maxWidth: 400, margin: "40px auto" }}>
      {user?.must_change_password && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="首次登录请先修改初始密码"
          description="管理员为你创建了账号和初始密码，修改后即可正常使用知识库。"
        />
      )}
      <Form onFinish={onFinish} size="large">
        <Form.Item name="old_password" rules={[{ required: true, message: "请输入旧密码" }]}>
          <Input.Password prefix={<LockOutlined />} placeholder="旧密码" />
        </Form.Item>
        <Form.Item
          name="new_password"
          rules={[
            { required: true, message: "请输入新密码" },
            { min: 8, message: "至少 8 位" },
            { pattern: /^(?=.*[A-Za-z])(?=.*\d).+$/, message: "需同时包含字母和数字" },
          ]}
        >
          <Input.Password prefix={<LockOutlined />} placeholder="新密码（至少 8 位，含字母和数字）" />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={loading} block>
            确认修改
          </Button>
        </Form.Item>
      </Form>
    </Card>
  );
};

export default ChangePasswordPage;
