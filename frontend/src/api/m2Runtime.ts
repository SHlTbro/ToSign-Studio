import { ENDPOINT_STYLE_M2_RUNTIME, fill } from '@/api/endpoints'
import type { M2Runtime, M2RuntimeBinding } from '@/types/domain'
import { isUserScrRecipe } from '@/api/userScrRecipe'

export function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
export function isRegistrationId(value: string): boolean { return /^[0-9a-f]{32}$/.test(value) }
export function runtimePath(styleId: number, registrationId: string, recipeId?: string): string {
  const path = fill(ENDPOINT_STYLE_M2_RUNTIME.path, { id: String(styleId) })
  if (registrationId && !isRegistrationId(registrationId)) throw new Error('运行环境注册编号无效；已阻止检查与创建，不会切换到 CPU')
  const query = new URLSearchParams()
  if (recipeId) query.set('recipe_id', recipeId)
  if (registrationId) query.set('runtime_registration_id', registrationId)
  return query.size ? `${path}?${query}` : path
}
export function runtimeBinding(config: Record<string, unknown> | null | undefined): M2RuntimeBinding | null {
  const binding = record(config?.runtime_binding)
  const identities = ['asset_identity_sha256', 'dependency_lock_sha256', 'execution_profile_sha256', 'environment_identity_sha256', 'binding_sha256']
  const keys = ['schema', 'registration_id', 'variant_id', 'device', 'capability_receipt_sha256', ...identities]
  const hash = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
  return binding && Object.keys(binding).length === keys.length && keys.every((key) => key in binding)
    && binding.schema === 'm2-runtime-binding/1' && typeof binding.registration_id === 'string' && isRegistrationId(binding.registration_id)
    && typeof binding.variant_id === 'string' && binding.variant_id.trim().length > 0 && (binding.device === 'cpu' || binding.device === 'cuda')
    && identities.every((key) => hash(binding[key]))
    && (hash(binding.capability_receipt_sha256) || (binding.device === 'cpu' && binding.capability_receipt_sha256 === null))
    ? binding as unknown as M2RuntimeBinding : null
}
export function sameBinding(left: unknown, right: unknown): boolean {
  const a = record(left); const b = record(right)
  return Boolean(a && b && Object.keys(a).length === Object.keys(b).length
    && Object.keys(a).every((key) => a[key] === b[key]))
}
/** Invalid historical bindings get a visible missing option, never a default CPU. */
export function registrationForConfig(config: Record<string, unknown>): string {
  if (isUserScrRecipe(config.recipe_id) && typeof config.runtime_registration_id === 'string') {
    return isRegistrationId(config.runtime_registration_id) ? config.runtime_registration_id : 'invalid-runtime-binding'
  }
  const binding = runtimeBinding(config)
  if (binding) return binding.registration_id
  if ('runtime_binding' in config || ![undefined, 'cpu'].includes(record(config.train)?.device as string | undefined)) return 'invalid-runtime-binding'
  return ''
}
export function selectionProblem(runtime: M2Runtime | null, registrationId: string, styleId: number | null): string | null {
  if (!runtime?.ready || !runtime.config) return null
  if (Number(runtime.config.style_id) !== styleId) return '服务端运行配置与当前风格档案不一致；已阻止创建'
  const binding = runtimeBinding(runtime.config)
  if (!registrationId) {
    if (runtime.selected_runtime_binding != null || 'runtime_binding' in runtime.config || ![undefined, 'cpu'].includes(record(runtime.config.train)?.device as string | undefined)) return '默认 CPU 配置与服务端运行绑定不一致；已阻止创建'
    return null
  }
  const option = runtime.runtime_options?.find((row) => row.registration_id === registrationId)
  if (!option?.ready || !option.environment_ready || !option.full_model_ready) return '所选注册环境尚未通过完整模型验证，或注册已不可用；已阻止创建'
  if (isUserScrRecipe(runtime.config.recipe_id) && !('runtime_binding' in runtime.config)) {
    return runtime.config.runtime_registration_id === registrationId ? recipeProblem(runtime.config) : 'SCR 注册选择已改变，请刷新'
  }
  if (!binding || binding.registration_id !== registrationId || !sameBinding(binding, runtime.selected_runtime_binding)
    || binding.device !== option.device || record(runtime.config.train)?.device !== binding.device) return '服务端配置未绑定所选注册环境；已阻止创建，不会回退到 CPU'
  return null
}
export function matchesRunRuntime(config: Record<string, unknown>, runtime: M2Runtime | null, registrationId: string): boolean {
  if (registrationForConfig(config) !== registrationId || !runtime?.config || Number(config.style_id) !== Number(runtime.config.style_id)) return false
  const bound = runtimeBinding(config)
  if (isUserScrRecipe(config.recipe_id) && isUserScrRecipe(runtime.config.recipe_id) && !('runtime_binding' in runtime.config)) {
    return config.recipe_id === runtime.config.recipe_id && ['snapshot_sha256', 'preparation_id', 'runtime_registration_id'].every(key => config[key] === runtime.config?.[key])
      && Boolean(bound && runtime.runtime_options?.some(row => row.registration_id === registrationId && row.ready && row.environment_ready && row.full_model_ready && row.device === bound.device))
  }
  return bound ? record(config.train)?.device === bound.device && sameBinding(bound, runtime.config.runtime_binding)
    : registrationId === '' && !('runtime_binding' in config) && record(config.train)?.device === 'cpu'
}

/** Check the known recipe envelope; the server remains responsible for proof/hash validation. */
export function recipeProblem(config: Record<string, unknown> | null | undefined): string | null {
  if (config && isUserScrRecipe(config.recipe_id)) {
    if (config && !('runtime_binding' in config)) {
      const allowed = ['engine', 'recipe_id', 'style_id', 'snapshot_sha256', 'preparation_id', 'runtime_registration_id', 'train']
      const selectionTrain = record(config.train)
      if (Object.keys(config).every(key => allowed.includes(key)) && config.engine === 'm2'
        && Number.isSafeInteger(config.style_id) && Number(config.style_id) > 0
        && ['snapshot_sha256', 'preparation_id'].every(key => typeof config[key] === 'string' && /^[0-9a-f]{64}$/.test(config[key] as string))
        && typeof config.runtime_registration_id === 'string' && isRegistrationId(config.runtime_registration_id)
        && (config.train === undefined || (selectionTrain && Object.keys(selectionTrain).every(key => key === 'max_seconds')
          && typeof selectionTrain.max_seconds === 'number' && Number.isFinite(selectionTrain.max_seconds)
          && selectionTrain.max_seconds > 0 && selectionTrain.max_seconds <= 86400))) return null
      return 'SCR 服务端 ID 选择不完整，请重新准备并刷新'
    }
    const train = record(config.train)
    const binding = runtimeBinding(config)
    if (config.engine === 'm2' && config.objective_profile === 'epsilon-offset-scr2n-user-v1'
      && Number.isSafeInteger(config.style_id) && Number(config.style_id) > 0
      && typeof config.snapshot_sha256 === 'string' && /^[0-9a-f]{64}$/.test(config.snapshot_sha256)
      && typeof config.preparation_id === 'string' && config.preparation_id.length > 0
      && typeof config.runtime_registration_id === 'string' && isRegistrationId(config.runtime_registration_id)
      && binding?.registration_id === config.runtime_registration_id && binding.device === train?.device
      && typeof train?.max_seconds === 'number' && Number.isFinite(train.max_seconds) && train.max_seconds > 0 && train.max_seconds <= 86400) return null
    return '个人快照 SCR 的服务端配方身份、快照、准备标识或注册绑定缺失；已阻止创建/启动'
  }
  const identity = { recipe_id: 'm2-control-corrected-v1', objective_profile: 'epsilon-offset-control-v1',
    preprocessing_profile: 'fontdiffuser-freetype128-blackwhite-v1', checkpoint_selection: 'terminal_fixed_budget' }
  const recipe = record(config?.training_recipe); const budget = record(config?.training_budget); const train = record(config?.train)
  const fixed = record(recipe?.fixed_train)
  const count = budget?.verified_train_count
  const seconds = train?.max_seconds
  if (!config || config.engine !== 'm2' || !recipe || !budget || !train || !fixed || Object.entries(identity).some(([key, expected]) => config[key] !== expected || recipe[key] !== expected)
    || recipe.schema !== 'm2-training-recipe/1' || typeof recipe.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(recipe.sha256)
    || budget.schema !== 'm2-control-budget/1' || typeof count !== 'number' || !Number.isSafeInteger(count) || count < 8
    || budget.batch !== 2 || budget.drop_last !== true || budget.steps_per_epoch !== Math.floor(count / 2)
    || budget.expected_complete_epochs !== 8 || budget.expected_optimizer_steps !== 8 * Math.floor(count / 2)
    || budget.terminal_validation_required !== true || !['cpu', 'cuda'].includes(train.device as string) || train.max_epochs !== 8 || train.batch !== 2
    || train.max_steps !== budget.expected_optimizer_steps || fixed.max_epochs !== 8 || fixed.batch !== 2
    || typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0 || seconds > 86400 || budget.max_seconds !== seconds) {
    return 'M2 配方或动态预算缺失、未识别或不完整；已阻止创建/启动，请由服务端重新校验，不会补写历史配置'
  }
  return null
}
