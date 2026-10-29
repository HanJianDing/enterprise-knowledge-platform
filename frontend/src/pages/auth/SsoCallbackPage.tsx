/** SSO 回调落点页（企业知识库改造 阶段4）
 *
 * 后端 /auth/sso/{provider}/callback 在完成"绑定或自动建号 + 发 token"后
 * 302 到这里，token 放在 URL fragment（# 后）——fragment 不进访问日志。
 * 本页读 fragment → 落 localStorage → 拉取用户信息 → 进入知识问答。
 */
import React, { useEffect, useState } from "react";
import { Card, Spin, Alert } from "antd";
import { useNavigate } from "react-router-dom";
import { authApi } from "../../services/authApi";
import { useAuthStore } from "../../stores/authStore";

const SsoCallbackPage: React.FC = () => {
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const { setTokens, setUser, logout } = useAuthStore();

  useEffect(() => {
    const handle = async () => {
      const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
      const accessToken = params.get("access_token");
      const refreshToken = params.get("refresh_token");
      // 产品化 C6 backlog LOW 项：读取完凭证立即清掉 URL 里的 fragment——
      // token 不残留在地址栏/历史记录/截图里，后退再前进也不会重新暴露
      window.history.replaceState(null, "", window.location.pathname);
      if (!accessToken || !refreshToken) {
        setError("登录回跳缺少凭证，请重新发起登录");
        return;
      }
      setTokens(accessToken, refreshToken);
      try {
        const user = await authApi.getMe();
        setUser(user);
        navigate("/", { replace: true });
      } catch {
        logout();
        setError("登录状态校验失败，请重新发起登录");
      }
    };
    handle();
  }, [navigate, setTokens, setUser, logout]);

  if (error) {
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: "15vh" }}>
        <Card style={{ width: 420 }}>
          <Alert type="error" showIcon message="登录失败" description={error} />
        </Card>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: "15vh" }}>
      <Card style={{ width: 420, textAlign: "center" }}>
        <Spin size="large" />
        <div style={{ marginTop: 16, color: "#888" }}>正在完成登录…</div>
      </Card>
    </div>
  );
};

export default SsoCallbackPage;
