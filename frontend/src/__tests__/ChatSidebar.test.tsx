import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import ChatSidebar from "../pages/chat/ChatSidebar";
import type { Conversation } from "../types/chat";

const convs: Conversation[] = [
  {
    id: "c1",
    title: "尺码问题",
    status: "active",
    message_count: 2,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "c2",
    title: "退换货咨询",
    status: "active",
    message_count: 5,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  },
];

const baseProps = {
  currentId: null,
  collapsed: false,
  onSelect: vi.fn(),
  onDelete: vi.fn(),
  onNewChat: vi.fn(),
  onRename: vi.fn(),
  onClearAll: vi.fn(),
};

describe("ChatSidebar 对话管理增强", () => {
  it("搜索框按标题过滤会话", async () => {
    const user = userEvent.setup();
    render(<ChatSidebar {...baseProps} conversations={convs} />);
    expect(screen.getByText("尺码问题")).toBeInTheDocument();
    expect(screen.getByText("退换货咨询")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("搜索历史对话..."), "尺码");
    expect(screen.getByText("尺码问题")).toBeInTheDocument();
    expect(screen.queryByText("退换货咨询")).toBeNull();
  });

  it("搜索无匹配时显示空态文案", async () => {
    const user = userEvent.setup();
    render(<ChatSidebar {...baseProps} conversations={convs} />);
    await user.type(screen.getByPlaceholderText("搜索历史对话..."), "不存在的词");
    expect(screen.getByText("没有匹配的对话")).toBeInTheDocument();
  });

  it("重命名：点编辑按钮出现输入框，回车提交 onRename", async () => {
    const user = userEvent.setup();
    const onRename = vi.fn();
    render(<ChatSidebar {...baseProps} conversations={convs} onRename={onRename} />);

    await user.click(screen.getAllByTitle("重命名")[0]!);
    const input = screen.getByDisplayValue("尺码问题");
    await user.clear(input);
    await user.type(input, "尺码咨询{enter}");
    expect(onRename).toHaveBeenCalledWith("c1", "尺码咨询");
  });

  it("重命名 Enter 提交后 blur 连触发不重复提交（防抖守卫）", async () => {
    const user = userEvent.setup();
    const onRename = vi.fn();
    render(<ChatSidebar {...baseProps} conversations={convs} onRename={onRename} />);

    await user.click(screen.getAllByTitle("重命名")[0]!);
    const input = screen.getByDisplayValue("尺码问题");
    await user.clear(input);
    await user.type(input, "尺码咨询");
    await user.keyboard("{Enter}");
    // Enter 与 blur 可能在同一轮状态更新前接连触发：守卫保证只提交一次
    await user.click(screen.getByPlaceholderText("搜索历史对话..."));
    expect(onRename).toHaveBeenCalledTimes(1);
    expect(onRename).toHaveBeenCalledWith("c1", "尺码咨询");
  });

  it("清空全部对话：二次确认后调用 onClearAll", async () => {
    const user = userEvent.setup();
    const onClearAll = vi.fn();
    render(<ChatSidebar {...baseProps} conversations={convs} onClearAll={onClearAll} />);

    await user.click(screen.getByRole("button", { name: /清空全部对话/ }));
    // Modal.confirm 的确认按钮（红字"清空"；antd 会给两个汉字间插空格）
    // 注意：查询限定在 Modal 容器内，避免匹配到侧栏的"清空全部对话"触发按钮
    const modal = document.querySelector(".ant-modal");
    expect(modal).not.toBeNull();
    const okBtn = within(modal as HTMLElement).getByRole("button", { name: /清\s*空/ });
    await user.click(okBtn);
    expect(onClearAll).toHaveBeenCalled();
  });

  it("无会话时清空按钮禁用", () => {
    render(<ChatSidebar {...baseProps} conversations={[]} />);
    expect(screen.getByRole("button", { name: /清空全部对话/ })).toBeDisabled();
  });
});
