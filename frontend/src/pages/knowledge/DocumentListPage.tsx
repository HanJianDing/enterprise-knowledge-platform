import React, { useEffect, useRef, useState } from "react";
import {
  Card, Table, Button, Space, Upload, Tag, Popconfirm,
  Input, Select, Row, Col, Statistic, Tabs, Modal, Form, TreeSelect,
  Drawer, Typography, Empty, Timeline, Alert,
} from "antd";
import {
  UploadOutlined, DeleteOutlined, ReloadOutlined, FileTextOutlined, PlusOutlined,
  EyeOutlined, ProfileOutlined, HistoryOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import type { ColumnsType } from "antd/es/table";
import { message } from "../../utils/message";
import PageHeader from "../../components/common/PageHeader";
import { documentApi } from "../../services/documentApi";
import { kbApi } from "../../services/kbApi";
import type { DocumentInfo, KnowledgeStats, KnowledgeCategory, ChunkInfo } from "../../types/document";
import type { KnowledgeBaseInfo } from "../../types/kb";

const { Text, Paragraph } = Typography;

/** 分块模式 → 展示文本（列表列与分块详情抽屉共用，保证措辞一致） */
const CHUNK_TYPE_LABEL: Record<string, string> = {
  structured: "结构化",
  faq: "FAQ/政策",
  long_text: "长文本",
  chat_log: "聊天记录",
  table: "表格",
  laws: "条款",
  default: "兜底",
};
const chunkTypeLabel = (t: string): string => CHUNK_TYPE_LABEL[t] || t;

/** 解析流水事件 → 展示标签与颜色（产品化 A3：上传解析是知识库最高频痛点） */
const STAGE_META: Record<string, { label: string; color: string }> = {
  upload: { label: "上传", color: "blue" },
  start: { label: "开始处理", color: "blue" },
  done: { label: "处理完成", color: "green" },
  error: { label: "处理失败", color: "red" },
  reprocess: { label: "重试入队", color: "orange" },
  superseded: { label: "已替换", color: "default" },
  restore: { label: "已回溯", color: "green" },
};
const stageLabel = (s: string): string => STAGE_META[s]?.label ?? s;
const stageColor = (s: string): string => STAGE_META[s]?.color ?? "gray";

const DocumentListPage: React.FC = () => {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState("docs");
  // 企业知识库阶段1：当前选中的知识库（文档/分类/统计都按库隔离）
  const [kbs, setKbs] = useState<KnowledgeBaseInfo[]>([]);
  const [selectedKb, setSelectedKb] = useState<string | undefined>();
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [searchText, setSearchText] = useState("");
  // 批次F 验收 3.16：列表分类筛选（TreeSelect 选中分类 ID）
  const [listCategoryId, setListCategoryId] = useState<string | undefined>();
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadParentCatId, setUploadParentCatId] = useState<string | undefined>();
  const [uploadSubCatId, setUploadSubCatId] = useState<string | undefined>();
  // 分块模式必选：不再默认"长文本"（此前用户未选择时静默按 long_text 分块，
  // 与所选模式不符，造成"选了结构化却变长文本"的困惑）
  const [uploadContentType, setUploadContentType] = useState<string | undefined>(undefined);

  // 最终提交的 category_id：子分类优先
  const uploadCategoryId = uploadSubCatId || uploadParentCatId;

  // 分类管理
  const [categories, setCategories] = useState<KnowledgeCategory[]>([]);

  // 当前选中父分类的子分类列表（必须在 categories state 之后）
  const selectedParent = categories.find((c) => c.id === uploadParentCatId);
  const subCategories = selectedParent?.children || [];
  const [catModalOpen, setCatModalOpen] = useState(false);
  const [catForm] = Form.useForm();

  // 版本历史（批次5 7b）
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionsDoc, setVersionsDoc] = useState<DocumentInfo | null>(null);
  const [versionList, setVersionList] = useState<
    { id: string; version_no: number; status: string; created_at: string | null; chunk_count: number }[]
  >([]);

  const handleOpenVersions = async (doc: DocumentInfo) => {
    setVersionsDoc(doc);
    setVersionsOpen(true);
    try {
      setVersionList(await documentApi.getVersions(doc.id));
    } catch (err: any) {
      message.error(err.response?.data?.detail || "版本历史加载失败");
    }
  };

  const handleRestoreVersion = async (versionId: string) => {
    try {
      const r = await documentApi.restoreVersion(versionId);
      message.success(`已回溯到第 ${r.version_no} 版`);
      setVersionsOpen(false);
      fetchDocs();
    } catch (err: any) {
      message.error(err.response?.data?.detail || "回溯失败");
    }
  };

  // 分块查看 Drawer
  const [chunkDrawerOpen, setChunkDrawerOpen] = useState(false);
  const [chunkDocName, setChunkDocName] = useState("");
  const [chunks, setChunks] = useState<ChunkInfo[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);

  // 解析流水弹窗（产品化 A3：点开看"上传→分块→嵌入"全过程）
  const [logDoc, setLogDoc] = useState<DocumentInfo | null>(null);

  // 验收 A6：上传/重切/重试后轮询刷新——后台处理完成时间不确定，
  // 只拉一次列表会一直卡"处理中"，直到切页面才刷新
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 质检修复：轮询回调是闭包，直接读 page/selectedKb 会捕获启动时的旧值——
  // 轮询期间用户翻页/切库会把表格覆盖成旧页数据；用 ref 始终取最新值
  const pageRef = useRef(page);
  const selectedKbRef = useRef(selectedKb);
  useEffect(() => { pageRef.current = page; }, [page]);
  useEffect(() => { selectedKbRef.current = selectedKb; }, [selectedKb]);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = () => {
    stopPolling(); // 多次上传合并为一轮轮询
    let rounds = 0;
    pollRef.current = setInterval(async () => {
      rounds += 1;
      const kbId = selectedKbRef.current;
      const curPage = pageRef.current;
      try {
        // 无状态过滤地查最近 100 条判断是否还有排队/处理中的文档
        const r = await documentApi.list({ page: 1, page_size: 100, kb_id: kbId });
        const busy = r.items.some(
          (d) => d.status === "uploaded" || d.status === "processing"
        );
        // 直接更新当前页数据（不闪表格 loading）
        setDocs(r.items.slice((curPage - 1) * 20, curPage * 20));
        setTotal(r.total);
        setStats(await documentApi.getStats(kbId));
        if (!busy || rounds >= 30) stopPolling(); // 无排队/处理中或 60 秒兜底
      } catch {
        stopPolling();
      }
    }, 2000);
  };

  // 卸载时清理轮询定时器
  useEffect(() => () => stopPolling(), []);

  const fetchKbs = async () => {
    try {
      const list = await kbApi.listKbs();
      setKbs(list);
      // 优先展示已有内容；刷新时仅保留仍存在的选择。
      setSelectedKb((cur) => list.some((kb) => kb.id === cur) ? cur : (list.find((kb) => kb.document_count > 0) ?? list[0])?.id);
    } catch {
      message.error("加载知识库列表失败");
    }
  };

  const fetchDocs = async (searchOverride?: string) => {
    if (!selectedKb) { setDocs([]); setTotal(0); return; }
    setLoading(true);
    try {
      const r = await documentApi.list({
        page,
        page_size: 20,
        status: statusFilter,
        // 修复：搜索时直接传新关键字（走 state 会读到上一次的值，永远慢一拍）
        search: (searchOverride ?? searchText) || undefined,
        kb_id: selectedKb,
        category_id: listCategoryId,
      });
      setDocs(r.items);
      setTotal(r.total);
    } catch {
      message.error("加载失败");
    } finally {
      setLoading(false);
    }
  };

  const fetchStats = async () => {
    try { setStats(await documentApi.getStats(selectedKb)); } catch {}
  };

  const fetchCategories = async () => {
    if (!selectedKb) { setCategories([]); return; }
    try { setCategories(await documentApi.getCategories(selectedKb)); } catch {}
  };

  useEffect(() => {
    fetchKbs();
  }, []);

  // 分页修复：原实现把"重置页码"放在监听 [page] 的同一 effect 里——
  // 点第 2 页 → page=2 → effect 触发 → 立刻 setPage(1) 又跳回第一页，永远翻不了页。
  // 现在只在知识库/状态筛选真正变化时才重置页码（用 ref 对比上一次的值），
  // 纯翻页只走拉取分支。
  const resetTriggerRef = useRef({ kb: selectedKb, status: statusFilter, cat: listCategoryId });
  useEffect(() => {
    const prev = resetTriggerRef.current;
    resetTriggerRef.current = { kb: selectedKb, status: statusFilter, cat: listCategoryId };
    const shouldReset = prev.kb !== selectedKb || prev.status !== statusFilter
      || prev.cat !== listCategoryId;
    if (shouldReset && page !== 1) {
      setPage(1); // 只重置页码；page 变化会再次触发本 effect 拉取第一页
      return;
    }
    setUploadParentCatId(undefined);
    setUploadSubCatId(undefined);
    fetchDocs();
    fetchStats();
    fetchCategories();
  }, [page, statusFilter, selectedKb, listCategoryId]);

  const handleUpload = async (file: File) => {
    if (!selectedKb) {
      message.warning("请先选择知识库");
      return false;
    }
    if (!uploadContentType) {
      message.warning("请先选择内容类型（FAQ/政策、表格数据、结构化数据、长文本或聊天记录）");
      return false;
    }
    setUploading(true);
    try {
      const r = await documentApi.upload(file, selectedKb, uploadCategoryId, uploadContentType);
      message.success(r.message || "上传成功，后台处理中...");
      fetchDocs();
      fetchStats();
      // 批次F 验收修复：上传可能自动建立主题分类（3.15）——立即刷新
      // 分类树（此前要手动刷新页面才能看到新建分类）
      fetchCategories();
      startPolling(); // 验收 A6：轮询直到处理完成，状态自动更新
    } catch (err: any) {
      message.error(err.response?.data?.detail || "上传失败");
    } finally {
      setUploading(false);
    }
    return false;
  };

  const handleDelete = async (id: string) => {
    try {
      await documentApi.delete(id);
      message.success("已删除");
      fetchDocs();
      fetchStats();
    } catch {
      message.error("删除失败");
    }
  };

  /** 重试处理失败的文档（企业知识库改造阶段6：重启恢复 + 手动重试双保险）；
   *  force=true 对已完成文档按当前分块模板重新分块（产品化 A1） */
  const handleReprocess = async (doc: DocumentInfo, force = false) => {
    try {
      await documentApi.reprocess(doc.id, force);
      message.success(force ? "已重新分块，处理中..." : "已重新进入处理队列");
      fetchDocs();
      fetchStats();
      startPolling(); // 验收 A6：重切/重试同样轮询到完成
    } catch (err: any) {
      message.error(err.response?.data?.detail || "重试失败");
    }
  };

  const handleCreateCategory = async () => {
    if (!selectedKb) {
      message.warning("请先选择知识库");
      return;
    }
    try {
      const values = await catForm.validateFields();
      await documentApi.createCategory({ ...values, kb_id: selectedKb });
      message.success("分类已创建");
      setCatModalOpen(false);
      catForm.resetFields();
      fetchCategories();
      fetchStats();
    } catch {}
  };

  const handleViewChunks = async (doc: DocumentInfo) => {
    setChunkDocName(doc.original_name);
    setChunkDrawerOpen(true);
    setChunksLoading(true);
    try {
      const r = await documentApi.getChunks(doc.id);
      setChunks(r.chunks);
    } catch {
      message.error("获取分块失败");
      setChunks([]);
    } finally {
      setChunksLoading(false);
    }
  };

  const handleDeleteCategory = async (id: string) => {
    try {
      await documentApi.deleteCategory(id);
      message.success("分类已删除");
      fetchCategories();
      fetchStats();
    } catch (err: any) {
      // 批次F 验收修复：透出后端 400 的中文提示（分类下有文档时
      // "还有 N 篇文档"护栏提示，此前被通用"删除失败"吞掉）
      message.error(err.response?.data?.detail || "删除失败");
    }
  };

  // 批次5 低#8：superseded 颜色（"已替换"标签）
const statusColor: Record<string, string> = {
    uploaded: "warning", processing: "processing",
    completed: "success", failed: "error",
  };

  const docColumns: ColumnsType<DocumentInfo> = [
    {
      title: "文件名", dataIndex: "original_name", key: "name", width: 260, ellipsis: true,
      render: (name: string, record) => <div className="document-name"><FileTextOutlined /><div><Text ellipsis={{ tooltip: name }} style={{ maxWidth: 225 }}>{name}</Text><small>版本 v{record.version_no || 1} · {record.file_type.toUpperCase()}</small></div></div>,
    },
    {
      // 批次F 验收：分类名已拼父-子完整路径（"会议室使用-制度"），列宽放宽
      title: "分类", dataIndex: "category_name", key: "cat", width: 150, ellipsis: true,
      render: (n: string) => <Text ellipsis={{ tooltip: n }} style={{ maxWidth: 125 }}>{n || "未分类"}</Text>,
    },
    {
      title: "类型", dataIndex: "file_type", key: "type", width: 70,
      render: (t: string) => <Tag>{t.toUpperCase()}</Tag>,
    },
    {
      title: "分块模式", dataIndex: "content_type", key: "contentType", width: 110,
      render: (t: string) => (
        <Tag color={t === "structured" ? "geekblue" : t === "faq" ? "purple" : t === "long_text" ? "cyan" : t === "table" ? "green" : t === "laws" ? "volcano" : "default"}>
          {chunkTypeLabel(t)}
        </Tag>
      ),
    },
    {
      title: "大小", dataIndex: "file_size", key: "size", width: 90,
      render: (s: number) => s > 1024 * 1024 ? `${(s / 1024 / 1024).toFixed(1)}MB` : `${(s / 1024).toFixed(1)}KB`,
    },
    {
      title: "状态", dataIndex: "status", key: "status", width: 90,
      render: (s: string) => <Tag color={statusColor[s]}>{s === "uploaded" ? "待处理" : s === "processing" ? "处理中" : s === "completed" ? "已完成" : s === "superseded" ? "已替换" : "失败"}</Tag>,
    },
    {
      title: "版本", key: "version", width: 70,
      render: (_, record) =>
        record.version_no > 1 ? (
          <Button
            type="link"
            size="small"
            onClick={() => handleOpenVersions(record)}
            style={{ padding: 0 }}
          >
            v{record.version_no}
          </Button>
        ) : (
          <span style={{ color: "#bbb" }}>v1</span>
        ),
    },
    {
      title: "重试", key: "reprocess", width: 70,
      render: (_, record) =>
        record.status === "failed" ? (
          <Button
            type="link"
            size="small"
            onClick={() => handleReprocess(record)}
          >
            重试
          </Button>
        ) : record.status === "completed" ? (
          <Popconfirm
            title="按当前分块模板重新分块"
            description="将删除旧分块并重新处理（会产生嵌入 API 调用），确认继续？"
            okText="重新分块"
            cancelText="取消"
            onConfirm={() => handleReprocess(record, true)}
          >
            <Button type="link" size="small">
              重切
            </Button>
          </Popconfirm>
        ) : null,
    },
    {
      title: "流水", key: "processLog", width: 70,
      render: (_, record) => (
        <Button
          type="link"
          size="small"
          icon={<ProfileOutlined />}
          onClick={() => setLogDoc(record)}
          style={{ padding: 0 }}
        >
          查看
        </Button>
      ),
    },
    {
      title: "分块", dataIndex: "chunk_count", key: "chunks", width: 80,
      render: (count: number, record: DocumentInfo) =>
        count > 0 ? (
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handleViewChunks(record)}
            style={{ padding: 0 }}
          >
            {count}
          </Button>
        ) : (
          <span style={{ color: "#ccc" }}>{count}</span>
        ),
    },
    {
      title: "上传时间", dataIndex: "created_at", key: "time", width: 150,
      render: (t: string) => new Date(t).toLocaleString("zh-CN"),
    },
    {
      title: "操作", key: "action", width: 110, fixed: "right",
      render: (_, record) => (
        <Space size={0}>
          <Button
            type="text"
            icon={<HistoryOutlined />}
            title="版本历史与对比"
            onClick={() => navigate(`/admin/documents/${record.id}/versions`)}
          />
          <Popconfirm title="确定删除此文档?" onConfirm={() => handleDelete(record.id)}>
            <Button type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const catColumns: ColumnsType<KnowledgeCategory> = [
    { title: "分类名", dataIndex: "name", key: "name" },
    { title: "描述", dataIndex: "description", key: "desc", render: (d: string) => d || "-" },
    {
      title: "子分类", dataIndex: "children", key: "children",
      render: (children: KnowledgeCategory[]) => children?.length || 0,
    },
    { title: "创建时间", dataIndex: "created_at", key: "time", width: 160, render: (t: string) => new Date(t).toLocaleString("zh-CN") },
    {
      title: "操作", key: "action", width: 80,
      render: (_, record) => (
        <Popconfirm title="确定删除此分类?" onConfirm={() => handleDeleteCategory(record.id)}>
          <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className="page-shell management-page">
      <PageHeader
        title="知识库管理"
        description="文档按知识库分库管理，上传后系统自动分块并建立索引"
      />

      {/* 知识库切换（企业知识库阶段1：文档/分类/统计均按所选库隔离） */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Text strong>当前知识库：</Text>
          <Select
            style={{ width: 240 }}
            placeholder="选择知识库"
            value={selectedKb}
            onChange={setSelectedKb}
            options={kbs.map((k) => ({
              label: `${k.name}（${k.document_count} 篇文档）`,
              value: k.id,
            }))}
          />
          <Button size="small" icon={<ReloadOutlined />} onClick={fetchKbs}>刷新库列表</Button>
          <Text type="secondary">
            在「知识库与权限」中创建知识库和分配访问权限
          </Text>
        </Space>
      </Card>

      {/* 统计卡片 */}
      {stats && (
        <Row gutter={[12, 12]} className="knowledge-stats" style={{ marginBottom: 24 }}>
          <Col span={4}><Card size="small"><Statistic title="文档总数" value={stats.total_documents} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="已完成" value={stats.completed_documents} valueStyle={{ color: "#3f8600" }} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="处理中" value={stats.processing_documents} valueStyle={{ color: "#1890ff" }} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="失败" value={stats.failed_documents} valueStyle={{ color: "#cf1322" }} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="总分块" value={stats.total_chunks} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="分类数" value={stats.categories_count} /></Card></Col>
        </Row>
      )}

      <Tabs activeKey={activeTab} onChange={setActiveTab} items={[
        {
          key: "docs",
          label: "📄 文档管理",
          children: (
            <>
              {/* 操作栏 */}
              <Card title="上传新文档" size="small" style={{ marginBottom: 16 }}>
                <Space wrap>
                  <Select
                    placeholder="父分类"
                    allowClear
                    style={{ width: 140 }}
                    value={uploadParentCatId}
                    onChange={(v) => { setUploadParentCatId(v); setUploadSubCatId(undefined); }}
                    options={categories.map((c) => ({ label: c.name, value: c.id }))}
                  />
                  <Select
                    placeholder="子分类"
                    allowClear
                    style={{ width: 140 }}
                    value={uploadSubCatId}
                    onChange={setUploadSubCatId}
                    disabled={!uploadParentCatId || subCategories.length === 0}
                    options={subCategories.map((c) => ({ label: c.name, value: c.id }))}
                  />
                  <Select
                    placeholder="内容类型"
                    style={{ width: 160 }}
                    value={uploadContentType}
                    onChange={setUploadContentType}
                    options={[
                      { label: "📋 FAQ/政策", value: "faq" },
                      { label: "📊 表格数据", value: "table" },
                      { label: "📑 结构化数据", value: "structured" },
                      { label: "📝 长文本", value: "long_text" },
                      { label: "💬 聊天记录", value: "chat_log" },
                    ]}
                  />
                  <Upload
                    beforeUpload={(file) => { handleUpload(file); return false; }}
                    showUploadList={false}
                    accept=".pdf,.txt,.md,.docx,.csv,.xlsx,.png,.jpg,.jpeg"
                  >
                    <Button type="primary" icon={<UploadOutlined />} loading={uploading}>
                      上传文档
                    </Button>
                  </Upload>
                </Space>
              </Card>
              <Card size="small" style={{ marginBottom: 16 }}>
                <Space wrap>
                  <Input.Search
                    placeholder="搜索文件名..."
                    onSearch={(v) => { setSearchText(v); setPage(1); fetchDocs(v); }}
                    style={{ width: 250 }}
                    allowClear
                  />
                  <Select placeholder="状态筛选" allowClear style={{ width: 130 }}
                    value={statusFilter} onChange={(v) => setStatusFilter(v)}
                    options={[
                      { label: "已完成", value: "completed" },
                      { label: "处理中", value: "processing" },
                      { label: "失败", value: "failed" },
                    ]}
                  />
                  {/* 批次F 验收 3.16：列表分类筛选（主题父分类含其形态子分类的文档） */}
                  <TreeSelect
                    placeholder="分类筛选"
                    allowClear
                    treeDefaultExpandAll
                    style={{ width: 200 }}
                    value={listCategoryId}
                    onChange={setListCategoryId}
                    treeData={categories.map(c => ({
                      title: c.name, value: c.id,
                      children: c.children?.map(ch => ({ title: ch.name, value: ch.id })),
                    }))}
                  />
                  <Button icon={<ReloadOutlined />} onClick={() => fetchDocs()}>刷新</Button>
                </Space>
              </Card>

              {/* 文档表格 */}
              <Card title="文档列表" extra={<Text type="secondary">展开行查看处理详情</Text>}>
                <Table columns={docColumns.filter((c) => !["type", "contentType", "size", "version", "reprocess", "processLog", "time"].includes(String(c.key)))}
                  dataSource={docs} rowKey="id" loading={loading} scroll={{ x: 820 }} tableLayout="fixed"
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无符合条件的文档，可调整筛选或上传文档" /> }}
                  expandable={{ expandedRowRender: (record) => <Table size="small" rowKey="id" pagination={false} dataSource={[record]}
                    columns={docColumns.filter((c) => ["contentType", "size", "version", "reprocess", "processLog", "time"].includes(String(c.key)))} scroll={{ x: 700 }} /> }}
                  pagination={{
                    current: page, total, pageSize: 20, onChange: setPage,
                    showTotal: (t) => `共 ${t} 个文档`,
                  }}
                />
              </Card>
            </>
          ),
        },
        {
          key: "categories",
          label: "🏷️ 分类管理",
          children: (
            <Card
              title="知识库分类"
              extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setCatModalOpen(true)}>新建分类</Button>}
            >
              <Table columns={catColumns} dataSource={categories} rowKey="id"
                scroll={{ x: 700 }}
                pagination={false}
                expandable={{ childrenColumnName: "children" }}
              />
            </Card>
          ),
        },
      ]} />

      {/* 版本历史 Drawer（批次5 7b）：版本链 + 旧版回溯 */}
      <Drawer
        title={
          <Space>
            <ProfileOutlined />
            <span>版本历史</span>
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 400 }}>
              — {versionsDoc?.original_name}
            </Text>
          </Space>
        }
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        width={460}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="重传同名文档自动生成新版本，旧版数据保留；回溯后旧版恢复为当前版本，检索只走当前版本。"
        />
        {versionList.map((v) => (
          <Card
            key={v.id}
            size="small"
            style={{ marginBottom: 8 }}
            title={`第 ${v.version_no} 版`}
            extra={
              v.status === "superseded" ? (
                <Popconfirm
                  title="回溯到此版本？"
                  description="当前版本将被标记为已替换，检索切换到该旧版本"
                  okText="回溯"
                  cancelText="取消"
                  onConfirm={() => handleRestoreVersion(v.id)}
                >
                  <Button type="link" size="small">回溯</Button>
                </Popconfirm>
              ) : (
                <Tag color={v.status === "completed" ? "green" : "blue"}>
                  {v.status === "completed" ? "当前版本" : v.status}
                </Tag>
              )
            }
          >
            <div style={{ fontSize: 12, color: "#888" }}>
              上传时间：{v.created_at ? new Date(v.created_at).toLocaleString() : "—"}
              ｜ {v.chunk_count} 块
            </div>
          </Card>
        ))}
      </Drawer>

      {/* 分块查看 Drawer */}
      <Drawer
        title={
          <Space>
            <FileTextOutlined />
            <span>分块详情</span>
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 400 }}>
              — {chunkDocName}
            </Text>
          </Space>
        }
        open={chunkDrawerOpen}
        onClose={() => { setChunkDrawerOpen(false); setChunks([]); }}
        width={680}
        loading={chunksLoading}
        extra={
          <Tag color="orange">{chunks.length} 个分块</Tag>
        }
      >
        {chunks.length === 0 && !chunksLoading ? (
          <Empty description="暂无分块数据" />
        ) : (
          chunks.map((chunk) => (
            <Card
              key={chunk.chunk_id}
              size="small"
              style={{ marginBottom: 12 }}
              title={
                <Space>
                  <Tag color="blue">#{chunk.chunk_index + 1}</Tag>
                  {chunk.chunk_type && (
                    <Tag color="geekblue">{chunkTypeLabel(chunk.chunk_type)}</Tag>
                  )}
                  {chunk.parent_category_id && chunk.parent_category_id !== chunk.category_id && (
                    <Tag color="orange">父分类</Tag>
                  )}
                  <Text
                    type="secondary"
                    style={{ fontSize: 11, fontFamily: "monospace" }}
                    copyable={{ text: chunk.chunk_hash }}
                  >
                    hash: {chunk.chunk_hash.slice(0, 12)}…
                  </Text>
                </Space>
              }
            >
              <Paragraph
                style={{
                  margin: 0,
                  whiteSpace: "pre-wrap",
                  fontSize: 13,
                  lineHeight: 1.7,
                  maxHeight: 200,
                  overflow: "auto",
                  background: "#fafafa",
                  padding: 10,
                  borderRadius: 6,
                  border: "1px solid #f0f0f0",
                }}
              >
                {chunk.content}
              </Paragraph>
            </Card>
          ))
        )}
      </Drawer>

      {/* 解析流水弹窗（产品化 A3） */}
      <Modal
        title={
          <Space>
            <ProfileOutlined />
            <span>解析流水</span>
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 400 }}>
              — {logDoc?.original_name}
            </Text>
          </Space>
        }
        open={!!logDoc}
        onCancel={() => setLogDoc(null)}
        footer={null}
        width={560}
      >
        {logDoc?.status === "failed" && logDoc.error_message && (
          <Alert
            type="error"
            showIcon
            message="处理失败原因"
            description={logDoc.error_message}
            style={{ marginBottom: 16 }}
          />
        )}
        {logDoc?.process_log && logDoc.process_log.length > 0 ? (
          <Timeline
            items={logDoc.process_log.map((e, i) => ({
              key: `${e.time}-${i}`,
              color: stageColor(e.stage),
              children: (
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <Tag color={stageColor(e.stage)} style={{ margin: 0 }}>
                      {stageLabel(e.stage)}
                    </Tag>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {e.time}
                    </Text>
                  </div>
                  <div style={{ marginTop: 4, fontSize: 13, wordBreak: "break-all" }}>
                    {e.detail}
                  </div>
                </div>
              ),
            }))}
          />
        ) : (
          <Empty description="暂无解析流水（历史数据）" />
        )}
      </Modal>

      {/* 分类创建 Modal */}
      <Modal title="新建分类" open={catModalOpen} onOk={handleCreateCategory}
        onCancel={() => { setCatModalOpen(false); catForm.resetFields(); }}
        okText="创建" cancelText="取消"
      >
        <Form form={catForm} layout="vertical">
          <Form.Item name="name" label="分类名称" rules={[{ required: true, message: "请输入分类名称" }]}>
            <Input placeholder="例如: 手机配件、家电产品" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="可选描述" rows={2} />
          </Form.Item>
          <Form.Item name="parent_id" label="父分类">
            <TreeSelect
              placeholder="无(顶级分类)"
              allowClear
              treeDefaultExpandAll
              treeData={categories.map(c => ({
                title: c.name, value: c.id,
                children: c.children?.map(ch => ({ title: ch.name, value: ch.id })),
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default DocumentListPage;
