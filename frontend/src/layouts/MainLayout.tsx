import React, { useEffect, useState } from "react";
import { Layout, Menu, Dropdown, Avatar, Space, Typography, Button, Tooltip, Drawer } from "antd";
import type { MenuProps } from "antd";
import {
  MessageOutlined,
  BookOutlined,
  UserOutlined,
  LogoutOutlined,
  KeyOutlined,
  DashboardOutlined,
  SmileOutlined,
  SafetyCertificateOutlined,
  ApartmentOutlined,
  ProfileOutlined,
  FileSearchOutlined,
  ExperimentOutlined,
  ApiOutlined,
  SlidersOutlined,
  ToolOutlined,
  MoonOutlined,
  SunOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../services/authApi";
import { useThemeStore, useThemeTokens } from "../theme/themeStore";
import { useIsMobile } from "../hooks/useIsMobile";

const { Sider } = Layout;
const { Text } = Typography;

const MainLayout: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, isAdmin, isAuditViewer, logout, setUser } = useAuthStore();
  const mode = useThemeStore((s) => s.mode);
  const toggleMode = useThemeStore((s) => s.toggleMode);
  const t = useThemeTokens();
  const mobile = useIsMobile(767);
  const [navCollapsed, setNavCollapsed] = useState(location.pathname.startsWith("/chat"));
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => {
    setNavCollapsed(location.pathname.startsWith("/chat"));
    setNavOpen(false);
  }, [location.pathname]);

  // 页面刷新时重新加载用户信息
  useEffect(() => {
    if (!user) {
      authApi.getMe()
        .then((u) => setUser(u))
        .catch(() => {
          // token 失效，跳转登录
          logout();
          navigate("/login");
        });
    }
  }, [user, setUser, logout, navigate]);

  const menuItems: MenuProps["items"] = [
    {
      key: "home",
      icon: <DashboardOutlined />,
      label: "工作台",
    },
    {
      key: "/chat",
      icon: <MessageOutlined />,
      label: "知识问答",
    },
    ...(isAdmin
      ? [
          {
            key: "/knowledge",
            icon: <BookOutlined />,
            label: "知识库管理",
          },
          {
            key: "/admin/knowledge-bases",
            icon: <SafetyCertificateOutlined />,
            label: "知识库与权限",
          },
          {
            key: "/admin/departments",
            icon: <ApartmentOutlined />,
            label: "部门管理",
          },
          {
            key: "/admin/dashboard",
            icon: <DashboardOutlined />,
            label: "系统管理",
          },
          {
            key: "/admin/feedback",
            icon: <SmileOutlined />,
            label: "满意度反馈",
          },
          {
            key: "/admin/users",
            icon: <UserOutlined />,
            label: "用户管理",
          },
          {
            key: "/admin/audit-logs",
            icon: <FileSearchOutlined />,
            label: "审计日志",
          },
          {
            key: "/admin/eval",
            icon: <ExperimentOutlined />,
            label: "检索评估",
          },
          {
            key: "/admin/graph",
            icon: <ApartmentOutlined />,
            label: "知识图谱",
          },
          {
            key: "/admin/raptor",
            icon: <ApartmentOutlined />,
            label: "摘要树",
          },
          {
            key: "/admin/rules",
            icon: <ProfileOutlined />,
            label: "审批规则",
          },
          {
            key: "/admin/api-keys",
            icon: <KeyOutlined />,
            label: "API Key",
          },
          {
            key: "/admin/retrieval",
            icon: <SlidersOutlined />,
            label: "检索调优",
          },
          {
            key: "/admin/mcp",
            icon: <ApiOutlined />,
            label: "外部接入",
          },
          {
            key: "/admin/mcp-tools",
            icon: <ToolOutlined />,
            label: "MCP 工具",
          },
        ]
      : isAuditViewer
        ? [
            // 产品化 C5：审计员只读审计，不暴露任何其他管理入口
            {
              key: "/admin/audit-logs",
              icon: <FileSearchOutlined />,
              label: "审计日志",
            },
          ]
        : []),
  ];

  // 子路由（/chat/:id、/agent/tickets/:id）也要高亮对应菜单项：取最长前缀匹配；
  // 工作台路由是 "/"，startsWith 会全中，单独判断
  const menuKeys = (menuItems ?? []).map((i) => String(i!.key));
  const selectedKey =
    location.pathname === "/"
      ? "home"
      // 批次F 验收修复：find 取第一个前缀命中会踩 key 前缀包含——
      // "/admin/mcp" 是 "/admin/mcp-tools" 的前缀，点 MCP 工具页时
      // 高亮仍停在"外部接入"（真实验收）。按长度降序取最长匹配
      : menuKeys
          .filter((k) => location.pathname.startsWith(k))
          .sort((a, b) => b.length - a.length)[0] ?? "";

  const userMenuItems: MenuProps["items"] = [
    {
      key: "password",
      icon: <KeyOutlined />,
      label: "修改密码",
      onClick: () => navigate("/profile/change-password"),
    },
    { type: "divider" as const },
    {
      key: "logout",
      icon: <LogoutOutlined />,
      label: "退出登录",
      onClick: () => {
        logout();
        navigate("/login");
      },
    },
  ];

  const group = (key: string, label: string, keys: string[]): NonNullable<MenuProps["items"]>[number] => ({
    key, type: "group", label, children: menuItems.filter((item) => keys.includes(String(item?.key))),
  });
  const groupedItems: MenuProps["items"] = isAdmin && (!navCollapsed || mobile) ? [
    group("workspace", "工作空间", ["home", "/chat"]),
    group("knowledge", "知识管理", ["/knowledge", "/admin/knowledge-bases", "/admin/graph", "/admin/raptor", "/admin/rules"]),
    group("quality", "质量与运营", ["/admin/dashboard", "/admin/feedback", "/admin/eval", "/admin/retrieval"]),
    group("settings", "组织与接入", ["/admin/users", "/admin/departments", "/admin/audit-logs", "/admin/api-keys", "/admin/mcp", "/admin/mcp-tools"]),
  ] : menuItems;
  const compact = navCollapsed && !mobile;

  const navigation = (
        <div
          className={compact ? "navigation-inner is-compact" : "navigation-inner"}
          style={{
            display: "flex",
            flexDirection: "column",
            height: "100vh",
            position: "sticky",
            top: 0,
          }}
        >
          {/* Logo 区 */}
          <div
            style={{
              padding: compact ? "20px 19px" : "20px",
              display: "flex",
              alignItems: "center",
              gap: 10,
              borderBottom: "1px solid rgba(255,255,255,0.08)",
            }}
          >
            <div
              style={{
                width: 34,
                height: 34,
                borderRadius: 9,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 17,
                fontWeight: 700,
                color: "#fff",
                background: "linear-gradient(135deg, #2563eb, #7c3aed)",
                boxShadow: "0 4px 12px rgba(37, 99, 235, 0.4)",
                flexShrink: 0,
              }}
            >
              知
            </div>
            {!compact && <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.3 }}>
              <span style={{ color: "#fff", fontWeight: 600, fontSize: 15 }}>
                企业知识库
              </span>
              <span style={{ color: "rgba(255,255,255,0.45)", fontSize: 10, letterSpacing: 1 }}>
                KNOWLEDGE BASE
              </span>
            </div>}
          </div>
          {/* 菜单区 */}
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={selectedKey ? [selectedKey] : []}
            inlineCollapsed={compact}
            items={groupedItems}
            onClick={({ key }) => { navigate(key === "home" ? "/" : key); setNavOpen(false); }}
            style={{ flex: 1, minHeight: 0, overflowY: "auto", background: "transparent", borderInlineEnd: "none", padding: "8px 0" }}
          />
          {/* 底部用户区 */}
          <div
            style={{
              padding: compact ? "12px 16px" : "16px",
              borderTop: "1px solid rgba(255,255,255,0.08)",
            }}
          >
            <Space direction={compact ? "vertical" : "horizontal"}>
              <Dropdown menu={{ items: userMenuItems }} placement="topRight" trigger={["click"]}>
                <button className="account-menu" aria-label="账户菜单">
                  <Avatar size="small" icon={<UserOutlined />} />
                  {!compact && <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.4 }}>
                    <Text
                      style={{ color: "#fff", fontSize: 13, maxWidth: 110 }}
                      ellipsis
                    >
                      {user?.username || "用户"}
                    </Text>
                    <Text style={{ color: "rgba(255,255,255,0.55)", fontSize: 11 }}>
                      {isAdmin ? "管理员" : isAuditViewer ? "审计员" : "员工"}
                    </Text>
                  </div>}
                </button>
              </Dropdown>
              <Tooltip title={mode === "light" ? "切换深色模式" : "切换浅色模式"}>
                <Button
                  type="text"
                  size="small"
                  icon={mode === "light" ? <MoonOutlined /> : <SunOutlined />}
                  onClick={toggleMode}
                  style={{ color: "rgba(255,255,255,0.75)" }}
                  aria-label="切换主题"
                />
              </Tooltip>
            </Space>
            {!mobile && <Button type="text" className="nav-toggle" aria-label={compact ? "展开导航" : "收起导航"}
              icon={compact ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setNavCollapsed(!compact)} />}
          </div>
        </div>
  );
  return (
    <Layout className="app-shell" style={{ minHeight: "100dvh" }}>
      {mobile ? <Drawer title="导航" placement="left" width={260} open={navOpen} onClose={() => setNavOpen(false)}
        styles={{ body: { padding: 0, background: t.color.siderFrom } }}>{navigation}</Drawer> :
        <Sider width={216} collapsedWidth={72} collapsed={navCollapsed} trigger={null}
          style={{ background: `linear-gradient(180deg, ${t.color.siderFrom}, ${t.color.siderTo})` }}>{navigation}</Sider>}
      <Layout style={{ minWidth: 0 }}>
        {mobile && <header className="mobile-shell-header"><Button aria-label="打开导航" type="text" icon={<MenuUnfoldOutlined />} onClick={() => setNavOpen(true)} /><strong>企业知识库</strong></header>}
        <Layout.Content className="app-content" style={{ background: t.color.layoutBg, minWidth: 0 }}>
          <Outlet />
        </Layout.Content>
      </Layout>
    </Layout>
  );
};

export default MainLayout;
