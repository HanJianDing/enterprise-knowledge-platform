import React, { useEffect, useState } from "react";
import { Card, Table, Tag, Alert, Input, Space, Button } from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import {
  adminApi,
  type ApprovalRuleConflict,
  type ApprovalRuleItem,
} from "../../services/adminApi";

/** 区间文本：数值+单位（统一换算后的元/天原样展示） */
const rangeText = (r: ApprovalRuleItem): string => {
  const num = (v: number | null): string => (v === null ? "" : `${v}`);
  if (r.min_value === null || r.min_value === 0) {
    return r.max_value === null ? "全部" : `${num(r.max_value)}${r.unit}以内`;
  }
  if (r.max_value === null) {
    return `${num(r.min_value)}${r.unit}以上`;
  }
  return `${num(r.min_value)} 至 ${num(r.max_value)}${r.unit}`;
};

/** 审批规则管理页（批次2-5）：规则腿解析产物可视化校验——
 * 文档解析出的审批链规则一览，管理员核对"解析对了没有"；
 * 批次3：顶部冲突告警（旧版/新版制度并存，新文档优先裁决） */
const ApprovalRulesPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [rules, setRules] = useState<ApprovalRuleItem[]>([]);
  const [conflicts, setConflicts] = useState<ApprovalRuleConflict[]>([]);
  const [approverInput, setApproverInput] = useState("");

  const loadRules = async (search?: string) => {
    setLoading(true);
    try {
      setRules(await adminApi.approvalRules(search || undefined));
      setConflicts(await adminApi.approvalRuleConflicts());
    } catch (err: any) {
      message.error(err.response?.data?.detail || "规则加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRules();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const columns: ColumnsType<ApprovalRuleItem> = [
    { title: "业务", dataIndex: "business_type", key: "biz", width: 90,
      render: (b: string) => <Tag color="blue">{b}</Tag> },
    { title: "区间", key: "range", width: 200, render: (_, r) => rangeText(r) },
    { title: "审批人/机构", dataIndex: "approver", key: "approver", width: 160 },
    { title: "动作", dataIndex: "action", key: "action", width: 80,
      render: (a: string) => <Tag color={a === "备案" ? "orange" : "green"}>{a}</Tag> },
    { title: "来源文档", dataIndex: "source_doc", key: "doc", width: 220 },
    { title: "规则原文", dataIndex: "source_text", key: "text" },
  ];

  return (
    <div>
      <PageHeader
        title="审批规则"
        description="规则腿解析产物（纯正则零 LLM）——文档上传后自动解析，此处可视化校验"
      />
      <Card>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="规则用于「报销 3000 元谁审批」类问题的确定性回答；解析不了的句式（宁缺勿滥）不在此列，相关问题自动回退向量检索。"
        />
        {conflicts.length > 0 && (
          <Card size="small" style={{ marginBottom: 16 }} title="⚠ 规则冲突（旧版/新版制度并存）">
            {conflicts.map((c, i) => (
              <Alert
                key={i}
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message={`${c.business_type} 重叠段 ${c.overlap ?? ""} 审批人不同`}
                description={(
                  <>
                    <div>规则A：{c.rule_a.source_text}（{c.rule_a.doc_name}）</div>
                    <div>规则B：{c.rule_b.source_text}（{c.rule_b.doc_name}）</div>
                    <div>裁决：以「{c.resolved_doc}」为准 → <b>{c.resolved_approver}</b></div>
                  </>
                )}
              />
            ))}
          </Card>
        )}
        {/* 批次F 验收修复：搜索框放表格上方——此前在页面顶部，72 条
            冲突告警把表格推到页面底部，搜索完看不到表格变化 */}
        <Space style={{ marginBottom: 16 }}>
          <Input.Search
            placeholder="搜索审批人（如：财务总监）"
            allowClear
            style={{ width: 280 }}
            value={approverInput}
            onChange={(e) => setApproverInput(e.target.value)}
            onSearch={(v) => loadRules(v.trim())}
          />
          <Button size="small" onClick={() => { setApproverInput(""); loadRules(); }}>
            重置
          </Button>
        </Space>
        <Table
          columns={columns}
          dataSource={rules}
          rowKey={(r) => r.id}
          loading={loading}
          pagination={false}
          size="small"
        />
      </Card>
    </div>
  );
};

export default ApprovalRulesPage;
