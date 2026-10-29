import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import StreamingMarkdown from "../components/chat/StreamingMarkdown";

describe("StreamingMarkdown（批次F 验收·引用标记着色）", () => {
  it("[规则] 与 [来源N] 渲染为带色 span 类", () => {
    const { container } = render(
      <StreamingMarkdown
        content={"[规则] 部门负责人审批 [来源2]。依据见 [来源2]。\n\n**加粗**正常渲染"}
      />,
    );
    const rule = container.querySelector(".kb-tag-rule");
    const sources = container.querySelectorAll(".kb-tag-source");
    expect(rule?.textContent).toBe("[规则]");
    expect(sources.length).toBe(2);
    expect(sources[0]?.textContent).toBe("[来源2]");
    // markdown 加粗不受影响
    expect(container.querySelector("strong")?.textContent).toBe("加粗");
  });

  it("[KG] 与 [网络] 同样着色（四色体系）", () => {
    const { container } = render(
      <StreamingMarkdown
        content={"[KG] 郑浩的上级是钱进。[网络] 阿尔法狗由 DeepMind 开发。"}
      />,
    );
    expect(container.querySelector(".kb-tag-kg")?.textContent).toBe("[KG]");
    expect(container.querySelector(".kb-tag-web")?.textContent).toBe("[网络]");
  });

  it("无标记文本不做替换", () => {
    const { container } = render(
      <StreamingMarkdown content="普通回答，没有任何标注" />,
    );
    expect(container.querySelector(".kb-tag")).toBeNull();
    expect(container.textContent).toContain("普通回答");
  });
});
