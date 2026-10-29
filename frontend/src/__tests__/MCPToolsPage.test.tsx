import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import MCPToolsPage from "../pages/admin/MCPToolsPage";

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockDelete = vi.fn();
const mockToggle = vi.fn();
const mockTest = vi.fn();

vi.mock("../services/adminApi", () => ({
  adminApi: {
    mcpServerList: (...args: unknown[]) => mockList(...args),
    mcpServerCreate: (...args: unknown[]) => mockCreate(...args),
    mcpServerDelete: (...args: unknown[]) => mockDelete(...args),
    mcpServerToggle: (...args: unknown[]) => mockToggle(...args),
    mcpServerTest: (...args: unknown[]) => mockTest(...args),
  },
}));

vi.mock("../utils/message", () => ({
  message: {
    success: vi.fn(), error: vi.fn(), info: vi.fn(),
    warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(),
  },
}));

describe("MCPToolsPage MCP 工具管理（批次F 挂清单 #13）", () => {
  beforeEach(() => {
    mockList.mockClear();
    mockCreate.mockClear();
    mockDelete.mockClear();
    mockToggle.mockClear();
    mockTest.mockClear();
    mockList.mockResolvedValue([
      {
        id: "srv-1", name: "oa", transport: "http",
        url: "http://oa.internal/mcp/", command: null, args: [],
        enabled: true, created_at: "2026-10-16T08:00:00",
      },
      {
        id: "srv-2", name: "files", transport: "stdio",
        url: null, command: "npx", args: ["-y", "some-pkg"],
        enabled: false, created_at: "2026-10-16T09:00:00",
      },
    ]);
    mockCreate.mockResolvedValue({
      id: "srv-3", name: "new-oa", transport: "http",
      url: "http://new.internal/mcp/", command: null, args: [],
      enabled: true, created_at: "2026-10-16T10:00:00",
    });
    mockTest.mockResolvedValue({
      ok: true, tools: [{ name: "search_kb", description: "检索知识库" }],
    });
  });

  it("列表展示双传输配置与启停状态", async () => {
    render(<MCPToolsPage />);
    expect(await screen.findByText("oa")).toBeInTheDocument();
    expect(screen.getByText("files")).toBeInTheDocument();
    expect(screen.getByText("http://oa.internal/mcp/")).toBeInTheDocument();
    // stdio 行展示命令+参数
    expect(screen.getByText("npx -y some-pkg")).toBeInTheDocument();
  });

  it("添加 http 服务", async () => {
    render(<MCPToolsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /添加服务/ }));
    await userEvent.type(screen.getByPlaceholderText(/服务名/), "new-oa");
    await userEvent.type(
      screen.getByPlaceholderText(/服务地址/), "http://new.internal/mcp/",
    );
    await userEvent.click(screen.getByRole("button", { name: /^添\s*加$/ }));
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith({
      name: "new-oa", transport: "http", url: "http://new.internal/mcp/",
    }));
  });

  it("添加 stdio 服务（参数按空格拆分）", async () => {
    render(<MCPToolsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /添加服务/ }));
    await userEvent.type(screen.getByPlaceholderText(/服务名/), "calc");
    // 切换传输方式为 stdio
    const select = screen.getByRole("combobox");
    await userEvent.click(select);
    await userEvent.click(await screen.findByText(/stdio（本地进程）/));
    await userEvent.type(screen.getByPlaceholderText(/启动命令/), "npx");
    await userEvent.type(
      screen.getByPlaceholderText(/参数（空格分隔）/), "-y calc-pkg /data",
    );
    await userEvent.click(screen.getByRole("button", { name: /^添\s*加$/ }));
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith({
      name: "calc", transport: "stdio", command: "npx",
      args: ["-y", "calc-pkg", "/data"],
    }));
  });

  it("测试连接成功展示工具列表", async () => {
    render(<MCPToolsPage />);
    const testBtns = await screen.findAllByRole("button", { name: /测试连接/ });
    await userEvent.click(testBtns[0]!);
    expect(await screen.findByText(/连接成功，可用工具 1 个/)).toBeInTheDocument();
    expect(screen.getByText("search_kb")).toBeInTheDocument();
    expect(mockTest).toHaveBeenCalledWith({
      name: "oa", transport: "http", url: "http://oa.internal/mcp/",
    });
  });

  it("测试连接失败展示错误", async () => {
    mockTest.mockResolvedValueOnce({
      ok: false, tools: [], error: "连接失败：ConnectError",
    });
    render(<MCPToolsPage />);
    const testBtns = await screen.findAllByRole("button", { name: /测试连接/ });
    await userEvent.click(testBtns[0]!);
    expect(await screen.findByText(/连接失败：ConnectError/)).toBeInTheDocument();
  });

  it("停用/删除走接口", async () => {
    render(<MCPToolsPage />);
    const toggles = await screen.findAllByRole("switch");
    await userEvent.click(toggles[0]!);
    await waitFor(() => expect(mockToggle).toHaveBeenCalledWith("srv-1", false));

    // 行内删除按钮（两行共两个）；Popconfirm 确认按钮最后出现
    const delBtns = screen.getAllByRole("button", { name: /删\s*除/ });
    await userEvent.click(delBtns[0]!);
    const confirmBtns = await screen.findAllByRole("button", { name: /^删\s*除$/ });
    await userEvent.click(confirmBtns[confirmBtns.length - 1]!);
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("srv-1"));
  });
});
