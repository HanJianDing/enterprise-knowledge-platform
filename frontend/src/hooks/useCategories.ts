import { useState, useEffect, useCallback } from "react";
import { documentApi } from "../services/documentApi";
import type { KnowledgeCategory } from "../types/document";

/**
 * 分类数据加载 hook
 *
 * 将"从后端获取知识库分类"封装起来：
 * - 自动处理 loading 状态
 * - 返回 reload 函数供外部手动刷新（增删分类后立即更新）
 * - 错误时不抛异常，由调用方通过 categories 为空来判断
 */
export function useCategories() {
  const [categories, setCategories] = useState<KnowledgeCategory[]>([]);
  const [loading, setLoading] = useState(true);

  const loadCategories = useCallback(async () => {
    setLoading(true);
    try {
      const data = await documentApi.getCategories();
      setCategories(data);
    } catch (err) {
      console.error("加载分类失败:", err);
      setCategories([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  return { categories, loading, reload: loadCategories };
}
