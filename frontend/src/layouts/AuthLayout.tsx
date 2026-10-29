import React from "react";
import { ConfigProvider, Layout, theme as antdTheme } from "antd";
import { Outlet } from "react-router-dom";
import { getAntdThemeToken, getPalette } from "../theme/tokens";

/** 登录/SSO 回调布局：深色渐变背景 + 漂浮光球（纯 CSS 动画，无图片依赖）。
 *
 * 左侧品牌区（产品名 + 一句定位），右侧表单卡片。移动端隐藏品牌区只留卡片。
 * 表单卡片是白底毛玻璃，这里局部强制浅色算法——暗色用户进登录页时表单控件
 * 仍按浅色渲染，不会出现"白字白底"（全局 darkAlgorithm 会被局部覆盖）。
 */
const AuthLayout: React.FC = () => {
  return (
    <Layout className="login-bg" style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      {/* 漂浮光球：品牌蓝/紫两团，营造纵深 */}
      <div className="login-orb" style={{ width: 420, height: 420, background: "#2563eb", top: "-120px", left: "-80px" }} />
      <div className="login-orb" style={{ width: 360, height: 360, background: "#7c3aed", bottom: "-100px", right: "-60px", animationDelay: "-6s" }} />

      <div
        style={{
          position: "relative",
          zIndex: 1,
          width: "100%",
          maxWidth: 960,
          padding: "24px 4vw",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 48,
        }}
      >
        {/* 品牌区（小屏隐藏） */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16 }} className="login-brand-panel">
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: 14,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 26,
                fontWeight: 700,
                color: "#fff",
                background: "linear-gradient(135deg, #2563eb, #7c3aed)",
                boxShadow: "0 8px 24px rgba(37, 99, 235, 0.45)",
              }}
            >
              知
            </div>
            <div>
              <div style={{ color: "#fff", fontSize: 26, fontWeight: 700, letterSpacing: 1 }}>
                企业知识库
              </div>
              <div style={{ color: "rgba(255,255,255,0.55)", fontSize: 13, letterSpacing: 2 }}>
                ENTERPRISE KNOWLEDGE BASE
              </div>
            </div>
          </div>
          <div style={{ color: "#fff", fontSize: 34, fontWeight: 600, lineHeight: 1.5, marginTop: 24 }}>
            让每一份制度都被读懂
            <br />
            让每一个回答都有出处
          </div>
          <div style={{ color: "rgba(255,255,255,0.6)", fontSize: 15, lineHeight: 1.9 }}>
            部门级权限隔离 · 混合检索重排 · 答案全程可溯源
          </div>
        </div>

        {/* 表单卡片（局部强制浅色主题） */}
        <div style={{ width: 420, maxWidth: "100%" }}>
          <ConfigProvider
            theme={{
              algorithm: antdTheme.defaultAlgorithm,
              token: getAntdThemeToken(getPalette("light")),
            }}
          >
            <Outlet />
          </ConfigProvider>
        </div>
      </div>
    </Layout>
  );
};

export default AuthLayout;
