export const DEFAULT_QUESTIONS = [
  "年假怎么申请？", "报销流程是什么？", "入职需要准备哪些材料？",
  "公司考勤制度是怎样的？", "如何连接公司 WiFi？", "IT 报障找谁？",
];
/** 兼容旧客服演示数据，其他管理员配置保持可用。 */
export function normalizeQuickQuestions(questions: string[]): string[] {
  const cleaned = [...new Set(questions.map((q) => q.trim()).filter((q) => q && q !== "有XL码的衣服吗？"))];
  return cleaned.length ? cleaned.slice(0, 6) : DEFAULT_QUESTIONS;
}
