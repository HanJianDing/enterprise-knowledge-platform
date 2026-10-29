import React from "react";
import { Popover } from "antd";
import { FileTextOutlined } from "@ant-design/icons";
import type { Citation } from "../../types/chat";
import { useThemeTokens } from "../../theme/themeStore";

interface Props {
  citations: Citation[];
  /** 点击引用标签打开来源详情抽屉（"查看上下文"定位） */
  onDetail?: (citation: Citation) => void;
}

const CitationPopover: React.FC<Props> = ({ citations, onDetail }) => {
  const t = useThemeTokens();

  if (!citations || citations.length === 0) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <div
        style={{
          fontSize: 12,
          color: t.color.textSecondary,
          marginBottom: 8,
          fontWeight: 500,
        }}
      >
        参考来源 · {citations.length}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {citations.map((c) => (
          <Popover
            key={c.index}
            title={
              <span>
                <FileTextOutlined /> {c.title}
              </span>
            }
            content={
              <div
                style={{
                  maxWidth: 400,
                  maxHeight: 200,
                  overflow: "auto",
                  fontSize: 13,
                  lineHeight: 1.6,
                  whiteSpace: "pre-wrap",
                }}
              >
                {c.category_path && (
                  <div style={{ color: "#999", fontSize: 12, marginBottom: 4 }}>
                    🗂 {c.category_path}
                  </div>
                )}
                {c.text}
              </div>
            }
            trigger="hover"
          >
            <button
              className="citation-chip"
              aria-label={`来源${c.index}：${c.title}`}
              onClick={() => onDetail?.(c)}
            >
              <span>{c.index}</span><span>{c.title || `来源${c.index}`}</span>
            </button>
          </Popover>
        ))}
      </div>
      {onDetail && (
        <div style={{ fontSize: 12, color: t.color.textTertiary, marginTop: 6 }}>
          点击来源查看引用详情
        </div>
      )}
    </div>
  );
};

export default CitationPopover;
