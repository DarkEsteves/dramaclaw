// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  initReactI18next: {
    type: "3rdParty",
    init: vi.fn(),
  },
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("dramaclaw-spec-render", () => ({
  SpecRenderer: () => null,
  SpecRendererProvider: ({ children }: { children: ReactNode }) => children,
  VideoDetailModal: () => null,
}));

import { MessageBubble, visibleAssistantOrderedPartsForMessageForTest } from "@/features/superchat/superchat-panel";
import type { ChatMessage } from "@/features/superchat/types";

const baseMessage: ChatMessage = {
  id: "message-1",
  role: "assistant",
  text: "A normal assistant response.",
  timestamp: 1,
};

const sharedProps = {
  onOpenDetail: vi.fn(),
  onOpenMedia: vi.fn(),
  pinned: false,
  onDelete: vi.fn(),
  onTogglePin: vi.fn(),
};

describe("MessageBubble presentation", () => {
  it("renders one terminal canvas card for saved canonical and executor parts", () => {
    const pending = { key: "bridge:request", bridgeKey: "request", turnId: "turn", workflowRunId: "run", applied: 0, openedUiActions: 0, errors: [], commandResults: [{ commandIndex: 0, type: "run_node_action", status: "pending", label: "生成方案", nodeId: "plan", action: "generate_production_plan_draft", output: { workflow_run_id: "run", reason: "native_action_accepted" } }] };
    const terminal = { ...pending, key: "bridge:request.executor.owner", bridgeKey: "request.executor.owner", applied: 1, commandResults: [{ ...pending.commandResults[0], status: "success", output: { workflow_run_id: "run", workflow_run_status: "completed" } }] };
    const message: ChatMessage = { ...baseMessage, text: "", turnId: "turn", parts: [
      { id: "canvas_feedback:bridge:request", type: "canvas_feedback", event: pending },
      { id: "canvas_feedback:bridge:request.executor.owner", type: "canvas_feedback", event: terminal },
    ] };
    const parts = visibleAssistantOrderedPartsForMessageForTest(message).filter(part => part.type === "canvas_feedback");
    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({ event: { bridgeKey: "request.executor.owner", commandResults: [{ status: "success" }] } });
    const { container } = render(<MessageBubble message={message} {...sharedProps} variant="freezone" />);
    expect(container.querySelectorAll("[data-canvas-command-feedback-signature]")).toHaveLength(1);
  });
  it("replaces a local approval with its matching feedback in ordered parts", () => {
    const feedback = { key: "local:turn", applied: 0, openedUiActions: 0, errors: [], commandResults: [{ commandIndex: 0, type: "validate", status: "success", label: "已执行" }] };
    const message: ChatMessage = { ...baseMessage, text: "", turnId: "turn", parts: [
      { id: "canvas_approval:local:turn", type: "canvas_approval", event: { key: "local:turn", bridgeKey: null, turnId: "turn", envelopes: [], plans: [] } },
      { id: "canvas_feedback:local:turn", type: "canvas_feedback", event: feedback },
    ] };
    expect(visibleAssistantOrderedPartsForMessageForTest(message).map(part => part.type)).toEqual(["canvas_feedback"]);
  });
  it("uses a low-contrast surface for the user bubble", () => {
    const { container } = render(
      <MessageBubble message={{ ...baseMessage, role: "user" }} {...sharedProps} />,
    );

    const bubble = container.querySelector("article > div > div");
    expect(bubble).toHaveClass("bg-white/[0.07]");
    expect(bubble).not.toHaveClass("bg-white/[0.12]");
  });

  it("renders a completed assistant response without a bubble surface", () => {
    const { container } = render(<MessageBubble message={baseMessage} {...sharedProps} />);
    const article = container.querySelector("article");

    expect(article).toHaveClass("max-w-full", "text-foreground");
    expect(article).not.toHaveClass("border", "rounded-[14px]", "bg-transparent", "px-4");
    expect(screen.getByRole("group", { name: "aiAssistant.actions.label" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "aiAssistant.actions.copy" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "aiAssistant.actions.more" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "aiAssistant.actions.details" })).not.toBeInTheDocument();
  });

  it("renders only agent directives as reference remix system messages", () => {
    const { container, rerender } = render(
      <MessageBubble
        message={{ ...baseMessage, role: "system", text: "原片分析已完成，结果已保存。正在整理复刻建议。",
          turnId: "reference-remix-run-1-notice-analysis-completed",
          raw: { metadata: { message_kind: "reference_remix_status", recipient: "user" } } }}
        {...sharedProps}
      />,
    );

    expect(screen.queryByText("原片分析已完成，结果已保存。正在整理复刻建议。")).not.toBeInTheDocument();
    expect(screen.queryByText("aiAssistant.systemNotice")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "aiAssistant.actions.label" })).not.toBeInTheDocument();

    rerender(<MessageBubble message={{ ...baseMessage, role: "system", text: "请根据已保存的原片分析继续拟定方向。",
      raw: { metadata: { message_kind: "reference_remix_agent_directive", recipient: "agent" } } }} {...sharedProps} />);
    expect(container.firstElementChild).toHaveClass("justify-end");
    expect(screen.getByText("aiAssistant.systemNotice")).toBeInTheDocument();

    rerender(<MessageBubble message={{ ...baseMessage, role: "system" }} {...sharedProps} />);
    expect(container.firstElementChild).toHaveClass("justify-start");
    expect(screen.queryByText("aiAssistant.systemNotice")).not.toBeInTheDocument();
  });

  it("hides completion actions while the assistant response is streaming", () => {
    render(<MessageBubble message={baseMessage} {...sharedProps} streaming />);

    expect(screen.queryByRole("group", { name: "aiAssistant.actions.label" })).not.toBeInTheDocument();
  });

  it("keeps semantic surfaces for errors and tool events", () => {
    const { container, rerender } = render(
      <MessageBubble
        message={{ ...baseMessage, text: "hermes returned no content" }}
        {...sharedProps}
      />,
    );

    expect(container.querySelector("article")).toHaveClass(
      "border-red-400/20",
      "bg-red-400/[0.06]",
    );

    rerender(
      <MessageBubble
        message={{ ...baseMessage, role: "tool", text: "Tool result" }}
        {...sharedProps}
      />,
    );
    expect(container.querySelector("article")).toHaveClass(
      "border-amber-500/20",
      "bg-amber-500/8",
    );
  });

  it("keeps the assistant surface neutral when a failed tool card is present", () => {
    const { container } = render(
      <MessageBubble
        message={{
          ...baseMessage,
          text: "素材工作包提交失败，请先根据返回的错误修正后再继续。",
          parts: [{
            id: "tool-status-material-failure",
            type: "tool_status",
            event: {
              id: "material-failure",
              role: "tool",
              text: "freezone_prepare_material_work\n\nUnprocessable Entity",
              timestamp: 1,
              raw: {
                type: "agent.tool.updated",
                name: "freezone_prepare_material_work",
                status: "failed",
                error: "Unprocessable Entity",
              },
            },
          }],
        }}
        {...sharedProps}
        variant="freezone"
      />,
    );

    const article = container.querySelector("article");
    expect(article).not.toHaveClass("border-red-400/20", "bg-red-400/[0.06]");
    expect(screen.getByText("待重新准备素材制作草稿")).toBeInTheDocument();
  });

  it("hides failed tool details from the chat while keeping the failure status", () => {
    const failureDetail = "请在现有生成参数界面确认实际需要的图片/视频模型、画幅和分辨率";
    const { rerender } = render(
      <MessageBubble
        message={{
          ...baseMessage,
          id: "failed-tool",
          text: "",
          parts: [{
            id: "tool-status:failed-tool",
            type: "tool_status",
            event: {
              id: "failed-tool-event",
              role: "tool",
              text: "",
              timestamp: 1,
              raw: {
                type: "agent.tool.updated",
                name: "dramaclaw.freezone_request_user_clarification",
                status: "failed",
                error: failureDetail,
              },
            },
          }],
        }}
        {...sharedProps}
      />,
    );

    expect(screen.getByText("确认生成参数失败")).toBeInTheDocument();
    expect(screen.queryByText(failureDetail)).not.toBeInTheDocument();

    rerender(
      <MessageBubble
        message={{
          ...baseMessage,
          id: "failed-tool-message",
          role: "tool",
          text: `freezone_request_user_clarification\n\n${failureDetail}`,
          raw: {
            type: "agent.tool.updated",
            name: "dramaclaw.freezone_request_user_clarification",
            status: "failed",
            error: failureDetail,
          },
        }}
        {...sharedProps}
      />,
    );

    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.queryByText(failureDetail)).not.toBeInTheDocument();
  });

  it("marks structured media cards for lightweight interaction feedback", () => {
    const mediaSpec = {
      type: "character_showcase",
      root: "root",
      elements: {
        root: { type: "Stack", children: ["portrait"], props: {} },
        portrait: {
          type: "Image",
          children: [],
          props: {
            src: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
            title: "沈月",
          },
        },
      },
    };
    const { container } = render(
      <MessageBubble
        message={{
          ...baseMessage,
          text: `<ui-spec>${JSON.stringify(mediaSpec)}</ui-spec>`,
        }}
        {...sharedProps}
      />,
    );

    expect(container.querySelector(".st-unified-media-card")).toHaveClass("st-ai-media-card");
  });
});


describe('reference remix system notice normalization', () => {
  it.each(['live', 'history', 'cache'])('keeps agent recipient directives available to the chat for %s', async (source) => {
    const { normalizeMessage } = await import('@/features/superchat/message');
    const directive = { role: 'system', text: 'internal reference JSON', metadata: { message_kind: 'reference_remix_agent_directive', recipient: 'agent' } };
    const input = source === 'cache' ? { role: 'system', text: directive.text, raw: directive } : source === 'live' ? { ...directive, ...directive.metadata, metadata: {} } : directive;
    expect(normalizeMessage(input)).not.toBeNull();
    expect(normalizeMessage({ ...directive, metadata: { ...directive.metadata, recipient: 'user' } })).not.toBeNull();
    expect(normalizeMessage({ role: 'system', text: '操作失败，请重试' })).not.toBeNull();
  });
});
