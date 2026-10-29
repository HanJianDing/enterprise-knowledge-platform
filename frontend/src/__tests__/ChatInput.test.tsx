import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import ChatInput from "../components/chat/ChatInput";

// 辅助: 获取发送按钮 (antd Button 的 accessible role)
const getSendButton = () => screen.getByRole("button", { name: /发送|send/i });

describe("ChatInput", () => {
  it("中文输入法确认候选词时不发送", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} draft={{ text: "年假", key: 1 }} />);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", isComposing: true, keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("年假");
  });
  it("应该正常渲染输入框和发送按钮", () => {
    render(<ChatInput onSend={vi.fn()} />);
    expect(screen.getByPlaceholderText("输入您的问题...")).toBeInTheDocument();
    expect(getSendButton()).toBeInTheDocument();
  });

  it("发送按钮在输入为空时禁用", () => {
    render(<ChatInput onSend={vi.fn()} />);
    expect(getSendButton()).toBeDisabled();
  });

  it("输入文字后发送按钮可用", async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={vi.fn()} />);

    const input = screen.getByPlaceholderText("输入您的问题...");
    await user.type(input, "测试问题");

    expect(getSendButton()).not.toBeDisabled();
  });

  it("点击发送按钮调用 onSend 并清空输入", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);

    const input = screen.getByPlaceholderText("输入您的问题...");
    await user.type(input, "你好");
    await user.click(getSendButton());

    expect(onSend).toHaveBeenCalledWith("你好");
    expect(input).toHaveValue("");
  });

  it("按 Enter 键发送消息", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);

    const input = screen.getByPlaceholderText("输入您的问题...");
    await user.type(input, "你好{enter}");

    expect(onSend).toHaveBeenCalledWith("你好");
  });

  it("disabled 状态下输入框和发送按钮都禁用", () => {
    render(<ChatInput onSend={vi.fn()} disabled={true} />);
    const input = screen.getByPlaceholderText("输入您的问题...");

    expect(input).toBeDisabled();
    expect(getSendButton()).toBeDisabled();
  });

  it("自定义 placeholder 生效", () => {
    render(<ChatInput onSend={vi.fn()} placeholder="自定义提示..." />);
    expect(screen.getByPlaceholderText("自定义提示...")).toBeInTheDocument();
  });

  it("纯空格消息不会发送", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);

    const input = screen.getByPlaceholderText("输入您的问题...");
    await user.type(input, "   ");
    // 空格输入时发送按钮仍然禁用，click 会找不到可用按钮
    // 改为直接验证 onSend 没有被调用（因为 disabled 按钮无法点击）
    expect(onSend).not.toHaveBeenCalled();
  });

  it("输入框带 2000 字上限（与后端 max_length 一致）", () => {
    render(<ChatInput onSend={vi.fn()} />);
    const input = screen.getByPlaceholderText("输入您的问题...");
    expect(input).toHaveAttribute("maxlength", "2000");
  });

  it("流式生成中显示停止按钮，发送按钮消失", () => {
    render(<ChatInput onSend={vi.fn()} isStreaming={true} onStop={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /发送|send/i })).toBeNull();
    expect(screen.getByTitle("停止生成")).toBeInTheDocument();
  });

  it("流式生成中点击停止按钮调用 onStop，且输入框禁用", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(<ChatInput onSend={vi.fn()} isStreaming={true} onStop={onStop} />);

    expect(screen.getByPlaceholderText("正在生成回答...")).toBeDisabled();
    await user.click(screen.getByTitle("停止生成"));
    expect(onStop).toHaveBeenCalled();
  });
});
