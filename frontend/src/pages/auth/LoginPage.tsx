import React, { useEffect, useState } from "react";
import { Card, Form, Input, Button, Divider } from "antd";
import { message } from "../../utils/message";
import { UserOutlined, LockOutlined, WechatOutlined, ScanOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { authApi } from "../../services/authApi";
import { useAuthStore } from "../../stores/authStore";

/** SSO 按钮展示名（provider name → 中文） */
const SSO_LABEL: Record<string, string> = {
  wecom: "企业微信扫码登录",
  test: "测试扫码登录（联调用）",
};

const LoginPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  // SSO 登录方式（企业知识库改造阶段4：后端配置了哪些 provider 就显示哪些）
  const [ssoProviders, setSsoProviders] = useState<string[]>([]);
  const navigate = useNavigate();
  const { login, setUser, kickedMessage, clearKickedMessage } = useAuthStore();

  useEffect(() => {
    authApi
      .getSsoProviders()
      .then((providers) => setSsoProviders(providers))
      .catch(() => {
        // 接口失败不阻塞密码登录，仅不显示扫码入口
      });
  }, []);

  const handleSsoLogin = async (provider: string) => {
    try {
      const { auth_url } = await authApi.getSsoUrl(provider);
      window.location.href = auth_url;
    } catch {
      message.error("获取扫码登录链接失败");
    }
  };

  // 顶号提示：被其他设备踢下线时，在登录页弹出一次原因
  useEffect(() => {
    if (kickedMessage) {
      message.error(kickedMessage);
      clearKickedMessage();
    }
  }, [kickedMessage, clearKickedMessage]);

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const authTokens = await authApi.login(values);
      login(authTokens);
      // 登录后获取用户角色信息
      const user = await authApi.getMe();
      setUser(user);
      message.success("登录成功");
      // 首登强制改密：管理员建号/批量导入的账号先跳改密页，否则进工作台
      if (authTokens.must_change_password || user.must_change_password) {
        navigate("/profile/change-password");
      } else {
        navigate("/");
      }
    } catch (err: any) {
      message.error(err.response?.data?.detail || "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card className="login-card" styles={{ body: { padding: "32px 36px" } }}>
      <div style={{ textAlign: "center", marginBottom: 24 }}>
        <div
          style={{
            width: 56,
            height: 56,
            margin: "0 auto 12px",
            borderRadius: 14,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 28,
            fontWeight: 700,
            color: "#fff",
            background: "linear-gradient(135deg, #2563eb, #7c3aed)",
            boxShadow: "0 8px 24px rgba(37, 99, 235, 0.35)",
          }}
        >
          知
        </div>
        <div style={{ fontSize: 20, fontWeight: 700 }}>企业知识库</div>
        <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 4 }}>
          企业 AI 知识问答平台 · 账号由管理员分配
        </div>
      </div>
      <Form onFinish={onFinish} size="large">
        <Form.Item name="username" rules={[{ required: true, message: "请输入用户名" }]}>
          <Input prefix={<UserOutlined />} placeholder="用户名" />
        </Form.Item>
        <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]}>
          <Input.Password prefix={<LockOutlined />} placeholder="密码" />
        </Form.Item>
        <Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            block
            size="large"
            style={{ height: 44, fontWeight: 600 }}
          >
            登 录
          </Button>
        </Form.Item>
      </Form>
      {/* 企业知识库改造阶段3：开放注册已关闭，账号由管理员在「用户管理」中创建/导入 */}
      {ssoProviders.length > 0 && (
        <>
          <Divider plain style={{ fontSize: 12, color: "#94a3b8" }}>或使用企业账号登录</Divider>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {ssoProviders.map((p) => (
              <Button
                key={p}
                block
                size="large"
                icon={p === "wecom" ? <WechatOutlined /> : <ScanOutlined />}
                onClick={() => handleSsoLogin(p)}
              >
                {SSO_LABEL[p] || `${p} 扫码登录`}
              </Button>
            ))}
          </div>
        </>
      )}
    </Card>
  );
};

export default LoginPage;
