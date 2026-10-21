// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it, vi } from "vitest";
import type { TFunction } from "i18next";

import { taskErrorMessage } from "@/task-center/task-errors";
import type { TaskState } from "@/task-center/types";

function task(partial: Partial<TaskState>): TaskState {
  return {
    task_id: "task_1",
    task_type: "build_characters",
    project_id: "project_1",
    episode: 0,
    status: "failed",
    created_at: "",
    updated_at: "",
    ...partial,
  } as TaskState;
}

describe("taskErrorMessage", () => {
  it("shows the current production plan timing issue and ignores stale details", () => {
    const t = vi.fn((key: string, options?: Record<string, string>) => {
      if (key === "tasks.planValidation.start") return "start";
      if (key === "tasks.planValidation.speechSourceCycle") {
        return `carrier=${options?.placement}, turn=${options?.turn}, edge=${options?.edge}`;
      }
      if (key === "tasks.planValidation.speechSourceSpansBeats") {
        return `spans carrier=${options?.placement}, beat=${options?.beat}, turn=${options?.turn}`;
      }
      return options?.defaultValue ?? key;
    }) as unknown as TFunction;
    const failure = {
      task_id: "task_1", error_code: "plan_validation_failed",
      issues: [{ code: "speech_source_timing_cycle", path: "/placements/0/start",
                 placement_id: "carrier", turn_id: "line-1" }],
    };
    const current = task({ task_type: "remix_generate_production_plan_draft",
      error: "plan_validation_failed", metadata: { production_plan_failure: failure } });
    expect(taskErrorMessage(current, t)).toBe("carrier=carrier, turn=line-1, edge=start");
    expect(taskErrorMessage({ ...current, metadata: {},
      result: { task_metadata: { production_plan_failure: failure } } }, t)).toBe(
        "carrier=carrier, turn=line-1, edge=start",
    );
    expect(taskErrorMessage({ ...current, task_id: "new-task" }, t)).not.toContain("carrier=carrier");
    expect(taskErrorMessage({ ...current, task_type: "build_characters" }, t)).not.toContain("carrier=carrier");
  });

  it("names the owning beat and turn for a cross-beat speech source", () => {
    const t = vi.fn((key: string, options?: Record<string, string>) => {
      if (key === "tasks.planValidation.speechSourceSpansBeats") {
        return `spans carrier=${options?.placement}, beat=${options?.beat}, turn=${options?.turn}`;
      }
      return options?.defaultValue ?? key;
    }) as unknown as TFunction;
    const failure = {
      task_id: "task_1", error_code: "plan_validation_failed",
      issues: [{ code: "speech_source_spans_beats", path: "/placements/0",
                 placement_id: "demo-video", beat_id: "demo", turn_id: "bye" }],
    };
    const current = task({ task_type: "remix_generate_production_plan_draft",
      error: "plan_validation_failed", metadata: { production_plan_failure: failure } });
    expect(taskErrorMessage(current, t)).toBe("spans carrier=demo-video, beat=demo, turn=bye");
  });

  it("uses i18n for a missing novel task error", () => {
    const tMock = vi.fn((key: string, options?: { defaultValue?: string }) => {
      if (key === "common.novelImportRequired") {
        return "Please import a novel first";
      }
      return options?.defaultValue ?? key;
    });
    const t = tMock as unknown as TFunction;

    expect(
      taskErrorMessage(
        task({
          error_code: "NOVEL_IMPORT_REQUIRED",
          error: "请先导入小说",
        }),
        t,
      ),
    ).toBe("Please import a novel first");
    expect(tMock).toHaveBeenCalledWith("common.novelImportRequired", {
      defaultValue: "请先导入小说",
    });
  });

  it("uses i18n for missing billing rule task errors", () => {
    const tMock = vi.fn((key: string, options?: { defaultValue?: string }) => {
      if (key === "common.billingRuleNotConfigured") {
        return "Billing rule is not configured.";
      }
      return options?.defaultValue ?? key;
    });
    const t = tMock as unknown as TFunction;

    expect(
      taskErrorMessage(
        task({
          error_code: "BILLING_RULE_NOT_CONFIGURED",
          error: "计费规则未配置，请联系管理员设置积分规则",
        }),
        t,
      ),
    ).toBe("Billing rule is not configured.");
    expect(tMock).toHaveBeenCalledWith("common.billingRuleNotConfigured", {
      defaultValue: "计费规则未配置，请联系管理员设置积分规则",
    });
  });

  it("uses only the nested provider message in failure notifications", () => {
    const t = vi.fn((key: string) => key) as unknown as TFunction;
    const raw =
      'DramaClawAPI image generation failed: HTTP 400: request_id=req-123; ' +
      'body={"error":{"message":"Content failed safety review. / 内容未通过安全审核。",' +
      '"type":"content_policy_violation","code":"moderation_blocked"}}';

    expect(taskErrorMessage(task({ error: raw }), t)).toBe(
      "Content failed safety review. / 内容未通过安全审核。",
    );
  });
});
