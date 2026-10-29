# MCP 接入指南（产品化 B2/B3）

本系统的知识库检索能力已通过 **MCP 标准协议**对外暴露：任何支持 MCP 的 AI 应用
（Claude Desktop、Cursor、企业其他 AI 产品）都可以带着账号凭证调用，
**检索范围严格限定在该账号的可见知识库**（部门级权限穿透），每次调用写审计日志。

## 一、端点与鉴权

- 端点：`http(s)://<部署地址>/mcp/`（后端 FastAPI 挂载，MCP_ENABLED=false 可关闭）
- 鉴权：HTTP 头 `Authorization: Bearer <access_token>`（本系统登录后签发的 JWT）
- 未携带 / 无效 / 已禁用账号的 token：调用被拒绝（fail-closed，绝不匿名放行）

## 二、提供的工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `search_kb` | question（必填）、top_k（1~10，默认 6） | 检索可见知识库，返回带 `[来源N]` 编号、文件名与引用指令的上下文文本 |
| `list_visible_kbs` | 无 | 列出该账号可访问的知识库与权限级别（查看/上传/管理） |

## 三、接入示例（Claude Desktop）

1. 登录系统获取 access_token（登录接口返回，或浏览器 F12 → Network 中复制）
2. 编辑 Claude Desktop 配置文件 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "enterprise-kb": {
      "type": "http",
      "url": "http://localhost:8000/mcp/",
      "headers": { "Authorization": "Bearer <你的access_token>" }
    }
  }
}
```

3. 重启 Claude Desktop，即可看到 `search_kb` / `list_visible_kbs` 两个工具

## 四、反过来：本系统调用外部 MCP 工具（产品化 B3）

`.env` 配置：

```
MCP_SERVERS=[{"name": "oa", "url": "http://oa.internal/mcp/"}]
```

员工提问含数字/日期或配置了外部服务时，问答流程会：
启发式闸门 → LLM 决策工具 → 程序执行（内置 calculator/datetime 本地执行，
外部工具走 MCP 客户端）→ 结果以【工具查询结果】注入生成上下文 → 审计留痕
（内置记 tool.call，外部记 mcp.tool_call，审计页可查"AI 调了什么"）。

## 五、安全设计（面试可讲）

1. **权限穿透**：MCP 调用复用本系统账号体系与部门-KB 权限，检索范围 fail-closed
2. **返回自带质量**：生成侧兜底（必须引用、禁止编造）下沉到检索侧
3. **审计留痕**：每次工具调用写审计日志，随事务提交
4. **工具白名单**：LLM 决策出的工具名必须在候选列表内（防幻觉任意调用）；
   内置计算器 ast 白名单求值（仅数字与四则运算）
5. **已知边界**：当前 MCP 凭证用登录 access_token（有有效期）；长期 MCP 凭证
   （专用 token 签发）列入后续迭代
