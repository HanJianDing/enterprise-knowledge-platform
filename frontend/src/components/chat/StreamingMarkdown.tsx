import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

// 质检 P1 修复：rehype-raw 必须配套 sanitize（LLM 回显内容含 KB 文档/
// 网络搜索/MCP 输出，未消毒的活 HTML 可注入 iframe/form/style/img）。
// 白名单仅放行 span + className（着色标记所需），其余 HTML 一律剥除
const sanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    span: [...((defaultSchema.attributes as Record<string, unknown[]>).span || []), "className"],
  },
};

interface Props {
  content: string;
  isStreaming?: boolean;
}

/** 引用标记着色（批次F 验收）：四类来源标注与正文同色会让用户
 * 误以为标注是正文一部分——预处理替换为带 class 的 span（rehype-raw
 * 渲染），CSS 控制颜色：规则=橙（确定性引擎）、来源=蓝（文档引用）、
 * KG=紫（知识图谱）、网络=青（web 搜索）。替换只命中完整标记文本。 */
function colorizeTags(text: string): string {
  return text
    .replace(/\[规则\]/g, '<span class="kb-tag kb-tag-rule">[规则]</span>')
    .replace(/\[来源(\d+)\]/g, '<span class="kb-tag kb-tag-source">[来源$1]</span>')
    .replace(/\[KG\]/g, '<span class="kb-tag kb-tag-kg">[KG]</span>')
    .replace(/\[网络\]/g, '<span class="kb-tag kb-tag-web">[网络]</span>');
}

const StreamingMarkdown: React.FC<Props> = ({ content, isStreaming }) => {
  return (
    <div className={`markdown-body ${isStreaming ? "streaming-cursor" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
      >
        {colorizeTags(content || (isStreaming ? " " : ""))}
      </ReactMarkdown>
    </div>
  );
};

export default StreamingMarkdown;
