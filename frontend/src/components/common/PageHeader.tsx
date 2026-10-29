import React from "react";
import { Typography } from "antd";

interface PageHeaderProps {
  title: string;
  description?: string;
  extra?: React.ReactNode;
}

/** 通用页面头部：标题 + 描述 + 右侧操作区
 *
 * 替换各页面旧的 "emoji 大标题" 模式，阶段 5 全面铺开。
 */
const PageHeader: React.FC<PageHeaderProps> = ({ title, description, extra }) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      marginBottom: 20,
      flexWrap: "wrap",
      gap: 12,
    }}
  >
    <div>
      <Typography.Title level={4} style={{ margin: 0 }}>
        {title}
      </Typography.Title>
      {description && (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {description}
        </Typography.Text>
      )}
    </div>
    {extra ? <div>{extra}</div> : null}
  </div>
);

export default PageHeader;
