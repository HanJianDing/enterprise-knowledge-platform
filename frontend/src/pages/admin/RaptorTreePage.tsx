import React, { useCallback, useEffect, useState } from "react";
import {
  Card, Collapse, Empty, Spin, Tag, Typography,
} from "antd";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi, type RaptorOverview } from "../../services/adminApi";

const { Text, Paragraph } = Typography;

/** RAPTOR 摘要树可视化页（批次B）：
 * 每个厚文档的摘要金字塔——层级、簇、摘要文本与子树规模一览 */
const RaptorTreePage: React.FC = () => {
  const [overview, setOverview] = useState<RaptorOverview | null>(null);
  const [loading, setLoading] = useState(false);

  // 批次B.5 P3-5：load 用 useCallback + effect 依赖挂 load（防 lint
  // 误报与未来改动时依赖漂移）
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setOverview(await adminApi.raptorOverview());
    } catch (err) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })
          ?.response?.data?.detail || "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <Spin style={{ display: "block", margin: "80px auto" }} />;
  }

  return (
    <div>
      <PageHeader
        title="摘要树"
        description="超长文档的摘要金字塔（RAPTOR）——聚类 + 逐层 LLM 摘要；宏观问题命中高层摘要后下钻原文条款"
      />
      {!overview || overview.documents.length === 0 ? (
        <Empty description="暂无摘要树——超过 5000 字的文档处理完成后自动建树" />
      ) : (
        overview.documents.map((doc) => {
          const maxLevel = Math.max(...doc.clusters.map((c) => c.tree_level));
          const levels = Array.from({ length: maxLevel }, (_, i) => i + 1);
          return (
            <Card
              key={doc.doc_id}
              title={(
                <span>
                  {doc.filename}
                  <Tag style={{ marginLeft: 8 }} color="blue">
                    {doc.clusters.length} 簇 · {maxLevel} 层
                  </Tag>
                </span>
              )}
              size="small"
              style={{ marginBottom: 16 }}
            >
              <Collapse
                size="small"
                items={levels.map((level) => ({
                  key: String(level),
                  label: `第 ${level} 层（${doc.clusters.filter((c) => c.tree_level === level).length} 个簇）`,
                  children: (
                    <div>
                      {doc.clusters
                        .filter((c) => c.tree_level === level)
                        .map((c) => (
                          <div key={c.cluster_id} style={{ marginBottom: 12 }}>
                            <Text strong code>{c.cluster_id}</Text>
                            <Text type="secondary" style={{ marginLeft: 8 }}>
                              覆盖 {c.child_count} 个子块
                            </Text>
                            <Paragraph
                              style={{
                                marginTop: 4,
                                marginBottom: 0,
                                background: "#fafafa",
                                padding: 8,
                                borderRadius: 4,
                              }}
                            >
                              {c.summary_preview || "（摘要文本缺失）"}
                            </Paragraph>
                          </div>
                        ))}
                    </div>
                  ),
                }))}
              />
            </Card>
          );
        })
      )}
    </div>
  );
};

export default RaptorTreePage;
