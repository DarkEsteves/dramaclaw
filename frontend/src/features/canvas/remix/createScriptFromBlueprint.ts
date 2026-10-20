import { createProductionPlanFromStory } from './createProductionPlan';

/** Each request creates a separate plan branch from the analyzed reference. */
export function createScriptFromBlueprint(storyId: string): string {
  return createProductionPlanFromStory(storyId);
}
