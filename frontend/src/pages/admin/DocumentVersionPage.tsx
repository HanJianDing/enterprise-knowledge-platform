import React, { useEffect, useMemo, useState } from "react";
import { Button, Card, Col, Empty, List, Popconfirm, Row, Select, Space, Tag, Typography } from "antd";
import { DiffOutlined, RollbackOutlined } from "@ant-design/icons";
import { useParams } from "react-router-dom";
import { diffLines } from "diff";
import PageHeader from "../../components/common/PageHeader";
import { documentApi } from "../../services/documentApi";
import { message } from "../../utils/message";

/** 版本链条目（批次5 7b 后端返回） */
interface VersionItem {
  id: string;
  version_no: number;
  status: string;
  created_at: string | null;
  chunk_count: number;
}

interface VersionContent {
  doc_id: string;
  version_no: number;
  filename: string;
  content_type: string;
  content: string;
}

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  completed: { color: "green", text: "当前版本" },
  superseded: { color: "orange", text: "已被替换" },
  failed: { color: "red", text: "处理失败" },
};

/** diff 行渲染：-红（删除）/ +绿（新增）/ 灰（未变） */
const DiffView: React.FC<{ oldText: string; newText: string }> = ({ oldText, newText }) => {
  const parts = useMemo(
    () => diffLines(oldText, newText),
    [oldText, newText],
  );
  return (
    <div
      style={{
        fontFamily: "Consolas, 'Courier New', monospace",
        fontSize: 13,
        maxHeight: 560,
        overflow: "auto",
        border: "1px solid #f0f0f0",
        borderRadius: 6,
        padding: 8,
      }}
    >
      {parts.map((p, i) => {
        const lines = p.value.split("\n");
        // diffLines 末尾常带一个空串（最后换行），渲染时滤掉
        const visible = p.value.endsWith("\n")
          ? lines.slice(0, -1)
          : lines;
        const bg = p.added
          ? "#e6ffed"
          : p.removed
            ? "#ffebe9"
            : "transparent";
        const prefix = p.added ? "+ " : p.removed ? "- " : "  ";
        const color = p.added ? "#167d3d" : p.removed ? "#c0392b" : "#444";
        return (
          <pre
            key={i}
            style={{ margin: 0, background: bg, color, whiteSpace: "pre-wrap" }}
          >
            {visible.map((line, j) => (
              <div key={j}>{prefix + line}</div>
            ))}
          </pre>
        );
      })}
    </div>
  );
};

/** 文档版本历史与差异对比页（批次F 前置·diff 视图）——
 * 版本链列表 + 任选两版行级 diff（diff 库现成算法）+ 旧版回溯。 */
const DocumentVersionPage: React.FC = () => {
  const { docId = "" } = useParams();
  const [versions, setVersions] = useState<VersionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [leftId, setLeftId] = useState<string>(""); // 旧版（对比基准）
  const [rightId, setRightId] = useState<string>(""); // 新版
  const [leftContent, setLeftContent] = useState<VersionContent | null>(null);
  const [rightContent, setRightContent] = useState<VersionContent | null>(null);
  const [restoring, setRestoring] = useState(false);

  const loadVersions = async () => {
    setLoading(true);
    try {
      const chain = await documentApi.getVersions(docId);
      setVersions(chain);
      // 默认选中：链尾（最新）为右，链尾前一版为左（length 检查
      // 已保证索引存在，非空断言消除 noUncheckedIndexedAccess 告警）
      if (chain.length >= 2) {
        setRightId(chain[chain.length - 1]!.id);
        setLeftId(chain[chain.length - 2]!.id);
      } else if (chain.length === 1) {
        setRightId(chain[0]!.id);
      }
    } catch (err: any) {
      message.error(err.response?.data?.detail || "版本链加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (docId) loadVersions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  useEffect(() => {
    if (!leftId) { setLeftContent(null); return; }
    documentApi
      .getVersionContent(leftId)
      .then(setLeftContent)
      // 质检 P3-5：拉取失败显式提示（此前静默置空会显示误导性"选择
      // 两个版本后显示差异"空态）
      .catch(() => {
        setLeftContent(null);
        message.error("版本内容加载失败，请稍后重试");
      });
  }, [leftId]);

  useEffect(() => {
    if (!rightId) { setRightContent(null); return; }
    documentApi
      .getVersionContent(rightId)
      .then(setRightContent)
      .catch(() => {
        setRightContent(null);
        message.error("版本内容加载失败，请稍后重试");
      });
  }, [rightId]);

  const handleRestore = async (id: string) => {
    setRestoring(true);
    try {
      await documentApi.restoreVersion(id);
      message.success("已回溯，该版本恢复为当前版本");
      await loadVersions();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "回溯失败");
    } finally {
      setRestoring(false);
    }
  };

  const options = versions.map((v) => ({
    value: v.id,
    label: `第 ${v.version_no} 版（${v.status === "completed" ? "当前" : "已替换"} · ${v.chunk_count} 块）`,
  }));

  const noTextContent =
    (leftContent && !leftContent.content) ||
    (rightContent && !rightContent.content);

  return (
    <div>
      <PageHeader
        title="文档版本对比"
        description="重传产生的历史版本行级对比（红=删除，绿=新增）；可把旧版回溯为当前版本"
      />
      <Row gutter={16}>
        <Col span={7}>
          <Card title="版本链" loading={loading}>
            <List
              dataSource={versions}
              locale={{ emptyText: <Empty description="暂无版本记录" /> }}
              renderItem={(v) => {
                const statusTag = STATUS_TAG[v.status];
                return (
                <List.Item
                  actions={[
                    v.status === "superseded" ? (
                      <Popconfirm
                        key="restore"
                        title="回溯该版本为当前版本？当前版本将被标记为已替换"
                        onConfirm={() => handleRestore(v.id)}
                      >
                        <Button
                          size="small"
                          icon={<RollbackOutlined />}
                          loading={restoring}
                        >
                          回溯
                        </Button>
                      </Popconfirm>
                    ) : null,
                  ]}
                >
                  <List.Item.Meta
                    title={
                      <Space>
                        第 {v.version_no} 版
                        {statusTag && (
                          <Tag color={statusTag.color}>
                            {statusTag.text}
                          </Tag>
                        )}
                      </Space>
                    }
                    description={
                      v.created_at
                        ? new Date(v.created_at).toLocaleString("zh-CN")
                        : ""
                    }
                  />
                  <Typography.Text type="secondary">
                    {v.chunk_count} 块
                  </Typography.Text>
                </List.Item>
                );
              }}
            />
          </Card>
        </Col>
        <Col span={17}>
          <Card
            title={
              <Space>
                <DiffOutlined />
                对比
                <Select
                  style={{ width: 300 }}
                  placeholder="旧版（基准）"
                  value={leftId || undefined}
                  options={options}
                  onChange={setLeftId}
                />
                <span>→</span>
                <Select
                  style={{ width: 300 }}
                  placeholder="新版"
                  value={rightId || undefined}
                  options={options}
                  onChange={setRightId}
                />
              </Space>
            }
          >
            {versions.length < 2 ? (
              <Empty description="该文档只有一个版本（重传同文档后才会产生历史版本）" />
            ) : noTextContent ? (
              <Empty description="所选版本为图片文档，无纯文本内容，不支持对比" />
            ) : leftContent && rightContent ? (
              <DiffView
                oldText={leftContent.content}
                newText={rightContent.content}
              />
            ) : (
              <Empty description="选择两个版本后显示差异" />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default DocumentVersionPage;
