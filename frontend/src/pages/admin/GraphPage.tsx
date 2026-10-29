import React, { useEffect, useState } from "react";
import {
  Card, Button, Space, Alert, Empty, Tag, Spin, Typography,
} from "antd";
import { ReloadOutlined, ClusterOutlined } from "@ant-design/icons";
import type { EChartsOption } from "echarts";
import EChart from "../../components/charts/EChart";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { adminApi, type GraphOverviewResult } from "../../services/adminApi";

const { Paragraph } = Typography;

/** 社区调色板（community_id 取模，与图例一致） */
const COMMUNITY_COLORS = [
  "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
  "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc", "#2f4554",
];

/** 概览数据 → ECharts graph option（力导向；导出便于单测，不含 React 依赖）。
 *
 * 批次F 验收修复：社区高亮改用 setOption 直改样式——highlightMembers
 * 非 null 时高亮成员保持原色+放大，其余节点置灰半透明（ECharts 6.1
 * 的 highlight/downplay 状态机批量高亮有回归：18 次 dispatch 只有
 * 部分节点生效；图例悬停联动同样绑错对象）。零黑盒：渲染即配置。
 */
export function buildGraphOption(
  data: GraphOverviewResult,
  highlightMembers: string[] | null = null,
): EChartsOption {
  const hasCommunities = data.communities.length > 0;
  const colorOf = (communityId: number | null) =>
    communityId === null
      ? "#c0c4cc"
      : COMMUNITY_COLORS[communityId % COMMUNITY_COLORS.length];
  const hl = highlightMembers ? new Set(highlightMembers) : null;

  return {
    tooltip: {
      // 挂清单 #9 修复：渲染文本转义——实体描述来自后端（知识库
      // 上传内容派生），ECharts tooltip 按 HTML 渲染，未转义文本
      // 存在理论注入面；escapeHtml 纯文本化后风险消除
      formatter: (params: unknown) => {
        const p = params as { dataType?: string; data?: { name?: string; description?: string; relation?: string; strength?: number; source?: string; target?: string } };
        const d = p.data || {};
        const esc = (s: string) => s
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
        if (p.dataType === "node") {
          return `${esc(d.name || "")}${d.description ? `（${esc(d.description)}）` : ""}`;
        }
        return `${esc(d.source || "")} -${esc(d.relation || "")}→ ${esc(d.target || "")}（强度 ${d.strength ?? "-"}）`;
      },
    },
    legend: hasCommunities
      ? {
          // 批次F 验收修复：字符串数组形式 legend 的标记色走 ECharts
          // 默认主题色板，与节点自定义色不一致（图例【黑】社区3 vs
          // 节点黄色）——对象数组显式指定 itemStyle.color 对齐
          data: data.communities.map((c) => ({
            name: `社区 ${c.id + 1}`,
            itemStyle: { color: colorOf(c.id) },
          })),
          top: 10,
        }
      : undefined,
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        label: { show: true, position: "right", fontSize: 12 },
        force: {
          repulsion: 320,
          edgeLength: [80, 180],
          gravity: 0.08,
        },
        lineStyle: { color: "#c8cdd6", width: 1.5, curveness: 0.08 },
        // 批次F 验收修复：禁用 emphasis 状态机——图例悬停不再联动
        // 高亮节点（6.1 联动绑错对象：悬停任意图例都高亮同一组）；
        // 节点 tooltip 不依赖 emphasis，不受影响。
        // 质检返工：echarts@6.1 类型未收录 emphasis.disabled 字段
        // （运行时支持，源码走读确认）——as any 收窄，不能删 disabled
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        emphasis: { disabled: true } as any,
        categories: data.communities.map((c) => ({ name: `社区 ${c.id + 1}` })),
        data: data.nodes.map((n) => ({
          name: n.name,
          description: n.description,
          symbolSize: (n.type === "person" ? 34 : 26) + (hl?.has(n.name) ? 8 : 0),
          itemStyle: hl
            ? hl.has(n.name)
              ? { color: colorOf(n.community_id), opacity: 1 }
              : { color: "#c0c4cc", opacity: 0.15 }
            : { color: colorOf(n.community_id) },
          category: hasCommunities && n.community_id !== null
            ? n.community_id : undefined,
        })),
        links: data.edges.map((e) => ({
          source: e.source,
          target: e.target,
          relation: e.relation,
          strength: e.strength,
          // 挂清单 #8 修复：高亮态下按边两端节点是否都在高亮集合
          // 内决定边样式——节点置灰但边保持原样会"灰点挂亮线"
          lineStyle: hl
            ? (hl.has(e.source) && hl.has(e.target)
              ? { color: "#8a93a6", width: 1.5 }
              : { color: "#e0e2e6", width: 1 })
            : undefined,
          label: { show: true, formatter: e.relation, fontSize: 10 },
        })),
      },
    ],
  };
}

/** 知识图谱可视化页（批次2-3）：力导向图（社区着色）+ 社区报告 + 手动重算；
 * 批次2-5：点击社区卡片在图中高亮该社区全部节点（下钻交互） */
const GraphPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [data, setData] = useState<GraphOverviewResult | null>(null);
  // 批次F 验收修复：社区高亮集合（setOption 直改样式方案，替代
  // ECharts 6.1 坏掉的 highlight/downplay 状态机）；null=无高亮
  const [highlightMembers, setHighlightMembers] = useState<string[] | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setData(await adminApi.graphOverview());
      // 质检返工 P2-2：刷新/重算后清高亮残留——成员可能已换社区或
      // 节点消失，旧高亮集合会让无选中态卡片的节点置灰
      setHighlightMembers(null);
    } catch (err: any) {
      message.error(err.response?.data?.detail || "图谱数据加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleRefreshCommunities = async () => {
    setRefreshing(true);
    try {
      const r = await adminApi.refreshGraphCommunities();
      message.success(`社区检测完成：${r.community_count} 个社区`);
      await load();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "社区检测失败");
    } finally {
      setRefreshing(false);
    }
  };

  /** 批次F 验收修复：点击社区卡片 → setOption 高亮该社区节点、其余淡化；
   * 再点同一社区 → 取消高亮恢复全图（toggle） */
  const toggleCommunity = (members: string[]) => {
    setHighlightMembers((prev) => {
      const same = prev !== null
        && prev.length === members.length
        && prev.every((m) => members.includes(m));
      return same ? null : members;
    });
  };

  const graphAvailable = (data?.nodes.length ?? 0) > 0;

  return (
    <div>
      <PageHeader title="知识图谱" description="实体关系力导向图与社区检测结果（Leiden 算法按连接密度自动发现圈子）" />
      <Card
        title="实体关系图"
        extra={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新数据</Button>
            <Button
              type="primary"
              icon={<ClusterOutlined />}
              loading={refreshing}
              onClick={handleRefreshCommunities}
            >
              重算社区
            </Button>
          </Space>
        )}
        style={{ marginBottom: 16 }}
      >
        {loading ? (
          <Spin style={{ display: "block", margin: "60px auto" }} />
        ) : !graphAvailable ? (
          <Alert
            type="info"
            showIcon
            message="图谱暂无数据"
            description="图谱开关未开启（Neo4j 密码未配置）或尚无文档完成建图。启动 Neo4j 并等待文档建图后刷新。"
          />
        ) : (
          <EChart
            option={buildGraphOption(data!, highlightMembers)}
            height={560}
          />
        )}
      </Card>
      {graphAvailable && (
        <Card title={`社区报告（${data?.communities.length ?? 0} 个）`}>
          {(data?.communities.length ?? 0) === 0 ? (
            <Empty description="尚未运行社区检测，点击右上角「重算社区」生成" />
          ) : (
            data!.communities.map((c) => (
              <Card
                key={c.id}
                size="small"
                style={{ marginBottom: 12, cursor: "pointer" }}
                onClick={() => toggleCommunity(c.members)}
                hoverable
                title={(
                  <Space>
                    <Tag color={COMMUNITY_COLORS[c.id % COMMUNITY_COLORS.length]}>
                      社区 {c.id + 1}
                    </Tag>
                    <span>{c.count} 人</span>
                    <span style={{ color: "#999", fontSize: 12 }}>（点击在图内高亮，再点取消）</span>
                  </Space>
                )}
              >
                <Tag>{c.members.join("、")}</Tag>
                <Paragraph style={{ marginBottom: 0, marginTop: 8 }}>
                  {c.summary}
                </Paragraph>
              </Card>
            ))
          )}
        </Card>
      )}
    </div>
  );
};

export default GraphPage;
