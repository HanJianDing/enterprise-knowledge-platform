import { Navigate, useLocation } from "react-router-dom";
import { Spin } from "antd";
import AuthLayout from "./layouts/AuthLayout";
import MainLayout from "./layouts/MainLayout";
import LoginPage from "./pages/auth/LoginPage";
import ChangePasswordPage from "./pages/auth/ChangePasswordPage";
import SsoCallbackPage from "./pages/auth/SsoCallbackPage";
import ChatPage from "./pages/chat/ChatPage";
import WorkbenchPage from "./pages/WorkbenchPage";
import DocumentListPage from "./pages/knowledge/DocumentListPage";
import DashboardPage from "./pages/admin/DashboardPage";
import FeedbackListPage from "./pages/admin/FeedbackListPage";
import UserManagementPage from "./pages/admin/UserManagementPage";
import KnowledgeBaseManagePage from "./pages/admin/KnowledgeBaseManagePage";
import DepartmentManagePage from "./pages/admin/DepartmentManagePage";
import AuditLogPage from "./pages/admin/AuditLogPage";
import EvalPage from "./pages/admin/EvalPage";
import GraphPage from "./pages/admin/GraphPage";
import RaptorTreePage from "./pages/admin/RaptorTreePage";
import ApprovalRulesPage from "./pages/admin/ApprovalRulesPage";
import DocumentVersionPage from "./pages/admin/DocumentVersionPage";
import ApiKeyPage from "./pages/admin/ApiKeyPage";
import McpCredentialPage from "./pages/admin/McpCredentialPage";
import MCPToolsPage from "./pages/admin/MCPToolsPage";
import RetrievalTuningPage from "./pages/admin/RetrievalTuningPage";
import { useAuthStore } from "./stores/authStore";

// 路由守卫组件
const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const token = localStorage.getItem("access_token");
  const user = useAuthStore((s) => s.user);
  const location = useLocation();
  if (!token) return <Navigate to="/login" replace />;
  // 首登强制改密：管理员建号/批量导入的账号改密前不允许进入任何业务页
  if (
    user?.must_change_password &&
    location.pathname !== "/profile/change-password"
  ) {
    return <Navigate to="/profile/change-password" replace />;
  }
  return <>{children}</>;
};

const AdminRoute = ({ children }: { children: React.ReactNode }) => {
  const token = localStorage.getItem("access_token");
  const user = useAuthStore((s) => s.user);
  if (!token) return <Navigate to="/login" replace />;
  // user 未加载完成（getMe 进行中）时渲染 loading，避免刷新页面闪烁误重定向
  if (!user) return <Spin style={{ display: "block", margin: "100px auto" }} />;
  if (user.role !== "admin") return <Navigate to="/chat" replace />;
  return <>{children}</>;
};

// 审计视图路由（产品化 C5）：admin 或 auditor 可进——审计员只读审计，
// 能看审计的人与能改权限的人分离
const AuditRoute = ({ children }: { children: React.ReactNode }) => {
  const token = localStorage.getItem("access_token");
  const user = useAuthStore((s) => s.user);
  if (!token) return <Navigate to="/login" replace />;
  if (!user) return <Spin style={{ display: "block", margin: "100px auto" }} />;
  if (user.role !== "admin" && user.role !== "auditor") {
    return <Navigate to="/chat" replace />;
  }
  return <>{children}</>;
};

export const routes = [
  {
    element: <AuthLayout />,
    children: [
      { path: "/login", element: <LoginPage /> },
      { path: "/sso-callback", element: <SsoCallbackPage /> },
    ],
  },
  {
    element: (
      <ProtectedRoute>
        <MainLayout />
      </ProtectedRoute>
    ),
    children: [
      { path: "/", element: <WorkbenchPage /> },
      { path: "/chat", element: <ChatPage /> },
      { path: "/chat/:id", element: <ChatPage /> },
      { path: "/profile/change-password", element: <ChangePasswordPage /> },
      {
        path: "/knowledge",
        element: (
          <AdminRoute>
            <DocumentListPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/dashboard",
        element: (
          <AdminRoute>
            <DashboardPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/feedback",
        element: (
          <AdminRoute>
            <FeedbackListPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/users",
        element: (
          <AdminRoute>
            <UserManagementPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/knowledge-bases",
        element: (
          <AdminRoute>
            <KnowledgeBaseManagePage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/departments",
        element: (
          <AdminRoute>
            <DepartmentManagePage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/audit-logs",
        element: (
          <AuditRoute>
            <AuditLogPage />
          </AuditRoute>
        ),
      },
      {
        path: "/admin/eval",
        element: (
          <AdminRoute>
            <EvalPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/graph",
        element: (
          <AdminRoute>
            <GraphPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/raptor",
        element: (
          <AdminRoute>
            <RaptorTreePage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/rules",
        element: (
          <AdminRoute>
            <ApprovalRulesPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/documents/:docId/versions",
        element: (
          <AdminRoute>
            <DocumentVersionPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/api-keys",
        element: (
          <AdminRoute>
            <ApiKeyPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/retrieval",
        element: (
          <AdminRoute>
            <RetrievalTuningPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/mcp",
        element: (
          <AdminRoute>
            <McpCredentialPage />
          </AdminRoute>
        ),
      },
      {
        path: "/admin/mcp-tools",
        element: (
          <AdminRoute>
            <MCPToolsPage />
          </AdminRoute>
        ),
      },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
];
