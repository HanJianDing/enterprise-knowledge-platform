import React from "react";
import { Collapse } from "antd";
import type { Citation } from "../../types/chat";

interface Props {
  citations: Citation[];
}

/** 管理员端来源详情面板：展示编号、分类路径、文档名与内容片段（默认折叠） */
const SourceDetailPanel: React.FC<Props> = ({ citations }) => {
  if (!citations?.length) return null;

  return (
    <Collapse
      size="small"
      style={{ marginTop: 12, background: "#fafafa" }}
      items={[
        {
          key: "source-detail",
          label: `📚 来源详情（${citations.length}）`,
          children: (
            <ul
              style={{
                margin: 0,
                paddingLeft: 18,
                fontSize: 12,
                color: "#666",
                lineHeight: 1.8,
              }}
            >
              {citations.map((c) => (
                <li key={c.index} style={{ marginBottom: 8 }}>
                  <div>
                    <b>[来源{c.index}]</b> {c.category_path || "未知分类"} · {c.title}
                  </div>
                  <div
                    style={{
                      color: "#999",
                      whiteSpace: "pre-wrap",
                      fontFamily: "inherit",
                    }}
                  >
                    {c.text}
                  </div>
                </li>
              ))}
            </ul>
          ),
        },
      ]}
    />
  );
};

export default SourceDetailPanel;
