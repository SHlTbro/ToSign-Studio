import { describe, expect, it } from 'vitest'
import { recipeProblem, registrationForConfig, runtimePath, selectionProblem } from '@/api/m2Runtime'
import { USER_SCR_RECIPE, WINDOWS_SCR_RECIPE, isUserScrRecipe } from '@/api/userScr'
import type { M2Runtime } from '@/types/domain'
const rid = 'a'.repeat(32)
const selection = { engine: 'm2', recipe_id: WINDOWS_SCR_RECIPE, style_id: 1,
  snapshot_sha256: 'b'.repeat(64), preparation_id: 'c'.repeat(64), runtime_registration_id: rid }
const runtime: M2Runtime = { ready: true, status: 'READY', config: selection, blockers: [],
  runtime_options: [{ registration_id: rid, variant_id: 'windows-cp312-cu128-user-scr2n-spatial-cpu-v1', device: 'cpu', status: 'READY', ready: true, environment_ready: true, full_model_ready: true }],
  capabilities: { create_m2_run: true, run_evaluation: false, ai_visual_review: false, ai_candidate_confirmation: false, offline_ai_review_import: false } }
describe('Explicit Windows SCR ID selections; no native qualification claimed', () => {
  it('preserves Windows and Linux as distinct recipes and restores the exact registration', () => {
    expect(isUserScrRecipe(WINDOWS_SCR_RECIPE)).toBe(true); expect(isUserScrRecipe(USER_SCR_RECIPE)).toBe(true)
    expect(WINDOWS_SCR_RECIPE).not.toBe(USER_SCR_RECIPE); expect(isUserScrRecipe('historical')).toBe(false)
    expect(registrationForConfig(selection)).toBe(rid)
    expect(runtimePath(1, rid, WINDOWS_SCR_RECIPE)).toContain(`recipe_id=${WINDOWS_SCR_RECIPE}`)
  })
  it('accepts a complete ID-only server selection only with qualified registration', () => {
    expect(recipeProblem(selection)).toBeNull(); expect(selectionProblem(runtime, rid, 1)).toBeNull()
    expect(selectionProblem({ ...runtime, runtime_options: [] }, rid, 1)).not.toBeNull()
    expect(selectionProblem(runtime, 'd'.repeat(32), 1)).not.toBeNull()
    expect(selectionProblem(runtime, rid, 2)).not.toBeNull()
  })
  it('rejects forged paths, stale IDs and nonfinite or altered budget fields', () => {
    for (const changed of [{ preparation_id: 'short' }, { snapshot_sha256: 'unknown' }, { snapshot_dir: 'E:/forged' },
      { train: { max_seconds: Infinity } }, { train: { max_seconds: 900, device: 'cuda' } }]) {
      expect(recipeProblem({ ...selection, ...changed })).not.toBeNull()
    }
    expect(recipeProblem({ ...selection, train: { max_seconds: 900 } })).toBeNull()
  })
})
