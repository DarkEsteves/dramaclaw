// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type { TFunction } from "i18next";
import {
  backendErrorCodeToastMessage,
  humanizeTaskError,
  providerErrorMessage,
} from "@/lib/api-errors";
import type { TaskState } from "./types";

const SAFE_ID = /^[A-Za-z0-9_.:-]{1,64}$/;
const SAFE_PATH = /^\/[A-Za-z0-9_/-]{0,95}$/;

function productionPlanFailureMessage(task: TaskState, t: TFunction): string | null {
  if (task.status !== "failed" || task.error !== "plan_validation_failed" ||
      !["remix_generate_production_plan_draft", "remix_generate_production_plan_arrangement"].includes(task.task_type)) {
    return null;
  }
  const result = task.result && typeof task.result === "object" && !Array.isArray(task.result)
    ? task.result as Record<string, unknown> : null;
  const metadata = task.metadata && "production_plan_failure" in task.metadata
    ? task.metadata : result?.task_metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return null;
  const failure = (metadata as Record<string, unknown>).production_plan_failure;
  if (!failure || typeof failure !== "object" || Array.isArray(failure)) return null;
  const detail = failure as Record<string, unknown>;
  if (detail.task_id !== task.task_id || detail.error_code !== "plan_validation_failed") return null;
  const issue = Array.isArray(detail.issues) ? detail.issues[0] : null;
  if (!issue || typeof issue !== "object" || Array.isArray(issue)) return null;
  const fields = issue as Record<string, unknown>;
  if (fields.code === "speech_source_timing_cycle" &&
      typeof fields.placement_id === "string" && SAFE_ID.test(fields.placement_id) &&
      typeof fields.turn_id === "string" && SAFE_ID.test(fields.turn_id) &&
      typeof fields.path === "string" && SAFE_PATH.test(fields.path)) {
    const edgeKey = fields.path.endsWith("/start") ? "start" :
      fields.path.endsWith("/end") ? "end" : "range";
    return t("tasks.planValidation.speechSourceCycle", {
      placement: fields.placement_id, turn: fields.turn_id,
      edge: t(`tasks.planValidation.${edgeKey}`),
    });
  }
  const placement = typeof fields.placement_id === "string" && SAFE_ID.test(fields.placement_id)
    ? fields.placement_id : null;
  const beat = typeof fields.beat_id === "string" && SAFE_ID.test(fields.beat_id)
    ? fields.beat_id : null;
  const turn = typeof fields.turn_id === "string" && SAFE_ID.test(fields.turn_id)
    ? fields.turn_id : null;
  const path = typeof fields.path === "string" && SAFE_PATH.test(fields.path) ? fields.path : null;
  if (fields.code === "speech_source_spans_beats" && placement && beat && turn) {
    return t("tasks.planValidation.speechSourceSpansBeats", { placement, beat, turn });
  }
  if (fields.code === "speech_source_turns_not_contiguous" && placement && beat) {
    return t("tasks.planValidation.speechSourceNotContiguous", { placement, beat, turn: turn ?? "" });
  }
  if (typeof fields.code === "string" && SAFE_ID.test(fields.code) && path) {
    return t("tasks.planValidation.otherIssue", { path, code: fields.code });
  }
  return null;
}

export function taskErrorMessage(task: TaskState, t: TFunction): string {
  const planMessage = productionPlanFailureMessage(task, t);
  if (planMessage) return planMessage;
  const localized = backendErrorCodeToastMessage(
    task.error_code,
    task.error || t("common.error"),
    t,
  );
  if (localized) return localized;
  if (task.error_code === "INSUFFICIENT_CREDITS") {
    return t("common.insufficientCredits", {
      defaultValue: task.error || t("common.error"),
    });
  }
  if (task.error_code === "BILLING_RULE_NOT_CONFIGURED") {
    return t("common.billingRuleNotConfigured", {
      defaultValue: task.error || t("common.error"),
    });
  }
  const providerMessage = providerErrorMessage(task.error);
  if (providerMessage) return providerMessage;
  return humanizeTaskError(task.error, t);
}
