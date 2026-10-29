import React, { useEffect, useState } from "react";
import { Drawer, Spin, Tag, Empty } from "antd";
import { FileTextOutlined, EnvironmentOutlined } from "@ant-design/icons";
import { documentApi } from "../../services/documentApi";
import type { ChunkInfo } from "../../types/document";
import type { Citation } from "../../types/chat";
import { useThemeTokens } from "../../theme/themeStore";

interface Props {
  citation: Citation | null;
  onClose: () => void;
}

/** 引用详情抽屉：点击引用标签后，展示该切片原文 + 上下相邻切片 + 文档内位置。
 *
 * "答案可查证"三件套的第 2 件：从"看到片段"升级到"看到上下文"——
 * 员工能确认答案没有被断章取义（内部知识库可信度的关键）。
 * 切片按 chunk_index 取附近 ±1 条；历史旧消息没有 doc_id/chunk_idx 时降级只展示引用自带片段。
 */
const SourceDetailDrawer: React.FC<Props> = ({ citation, onClose }) => {
  const t = useThemeTokens();
  const [chunks, setChunks] = useState<{ total: number; chunks: ChunkInfo[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!citation) return;
    setChunks(null);
    setError(null);
    // 旧消息降级：只展示引用自带片段，不请求
    if (citation.doc_id == null || citation.chunk_idx == null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    // cancelled 标志：快速切换引用标签时丢弃旧响应，防止旧结果覆盖新切片
    let cancelled = false;
    documentApi
      .getChunks(citation.doc_id, citation.chunk_idx, 1)
      .then((r) => {
        if (!cancelled) {
          setChunks({ total: r.total, chunks: [...r.chunks].sort((a, b) => a.chunk_index - b.chunk_index) });
        }
      })
      .catch(() => {
        if (!cancelled) setError("切片上下文加载失败（文档可能已删除，或无权限）");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [citation]);

  return (
    <Drawer
      title={
        <span>
          <FileTextOutlined style={{ marginRight: 8 }} />
          来源详情
        </span>
      }
      width={560}
      open={!!citation}
      onClose={onClose}
    >
      {citation && (
        <div>
          {/* 文档信息头 */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: t.color.textPrimary, wordBreak: "break-all" }}>
              {citation.title}
            </div>
            <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
              {citation.category_path && (
                <Tag color="geekblue" style={{ margin: 0 }}>
                  🗂 {citation.category_path}
                </Tag>
              )}
              {chunks && citation.chunk_idx != null && (
                <Tag icon={<EnvironmentOutlined />} color="blue" style={{ margin: 0 }}>
                  文档内第 {citation.chunk_idx + 1} / {chunks.total} 块
                </Tag>
              )}
            </div>
          </div>

          {/* 旧消息降级：无 doc_id/chunk_idx，只展示引用自带片段 */}
          {citation.chunk_idx == null ? (
            <div
              style={{
                padding: 14,
                borderRadius: 8,
                background: t.color.primaryBg,
                border: `1px solid ${t.color.primaryBorder}`,
                fontSize: 14,
                lineHeight: 1.8,
                whiteSpace: "pre-wrap",
                color: t.color.textPrimary,
              }}
            >
              {citation.text}
              <div style={{ marginTop: 10, fontSize: 12, color: t.color.textTertiary }}>
                （历史消息仅保存了引用片段，无文档上下文）
              </div>
            </div>
          ) : loading ? (
            <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
              <Spin tip="加载切片上下文..." />
            </div>
          ) : error ? (
            <Empty description={error} />
          ) : chunks && chunks.chunks.length > 0 ? (
            /* 上下文切片列表：引用块高亮，相邻块降亮度 */
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {chunks.chunks.map((c) => {
                const isCited = c.chunk_index === citation.chunk_idx;
                return (
                  <div
                    key={c.chunk_id}
                    style={{
                      padding: "12px 14px",
                      borderRadius: 8,
                      background: isCited ? t.color.primaryBg : t.color.codeBg,
                      borderLeft: `3px solid ${isCited ? t.color.primary : t.color.borderLight}`,
                      opacity: isCited ? 1 : 0.75,
                    }}
                  >
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        marginBottom: 6,
                        color: isCited ? t.color.primaryText : t.color.textTertiary,
                      }}
                    >
                      {isCited ? "◎ 引用片段" : c.chunk_index < citation.chunk_idx! ? "上文片段" : "下文片段"}
                      <span style={{ fontWeight: 400, marginLeft: 8 }}>
                        第 {c.chunk_index + 1} 块
                      </span>
                    </div>
                    <div
                      style={{
                        fontSize: 14,
                        lineHeight: 1.8,
                        whiteSpace: "pre-wrap",
                        color: t.color.textPrimary,
                      }}
                    >
                      {c.content}
                    </div>
                  </div>
                );
              })}
              <div style={{ fontSize: 12, color: t.color.textTertiary }}>
                💡 上下文为引用块前后各 1 块，共 {chunks.total} 块切片
              </div>
            </div>
          ) : (
            <Empty description="该文档没有切片内容" />
          )}
        </div>
      )}
    </Drawer>
  );
};

export default SourceDetailDrawer;
