/* frozen-gated: true —— 本文件是 frozen 不变量测试（非页面消费组件），需引用
   frozen:false 符号做断言；消费侧门规则对页面/组件照常生效。 */
/**
 * endpoints 不变量（B5-02 修订口径）：
 * - 原 79 个端点 + Native intake 十端点 = 89 个端点；
 * - T33-B 七端点由 B-GATE-STYLE-API 与 OpenAPI v5 冻结后全部可消费；
 * - 其余（含 CC-4 ocr/extract 与 B5-02 解冻的 detect-text/extract）一律 frozen:true；
 * - 路径全部以 /api/v1 起；fill() 插值正确。
 */
import { describe, expect, it } from 'vitest'
import {
  ENDPOINT_AUTH_CHANGE_PASSWORD,
  ENDPOINT_AUTH_LOGIN,
  ENDPOINT_AUTH_LOGOUT,
  ENDPOINT_AUTH_ME,
  ENDPOINT_BACKUP_CREATE,
  ENDPOINT_BACKUP_DETAIL,
  ENDPOINT_BACKUP_RESTORE,
  ENDPOINT_BACKUP_VERIFY,
  ENDPOINT_BACKUPS,
  ENDPOINT_PHRASE_TEMPLATES,
  ENDPOINT_COVERAGE_CHECK,
  ENDPOINT_DETECT_TEXT,
  ENDPOINT_EXTRACT,
  ENDPOINT_OCR_EXTRACT,
  ENDPOINT_REGISTRY,
  ENDPOINT_STYLE_RUN_BLIND_REVIEW,
  ENDPOINT_STYLE_RUN_EVALUATE,
  ENDPOINT_STYLE_RUN_METRICS,
  ENDPOINT_STYLE_RUN_PROGRESS,
  ENDPOINT_STYLE_RUN_SAMPLE_DELETE,
  ENDPOINT_STYLE_RUN_SAMPLES,
  ENDPOINT_STYLE_RUN_UPDATE,
  ENDPOINT_TRAINING_SAMPLES,
  ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW,
  ENDPOINT_USER_EXPORT,
  ENDPOINT_WORKERS_STATUS,
  fill,
} from '@/api/endpoints'

describe('endpoints 全量登记与 frozen 纪律', () => {
  it('端点总数 = 原 96 个 + recovery/retry/resume 三端点 = 99', () => {
    expect(ENDPOINT_REGISTRY).toHaveLength(99)
  })

  it('T33-B 七扩展端点已由已发布 STYLE API Gate 冻结', () => {
    const styleRunExtensions = [
      ENDPOINT_STYLE_RUN_METRICS,
      ENDPOINT_STYLE_RUN_SAMPLES,
      ENDPOINT_STYLE_RUN_SAMPLE_DELETE,
      ENDPOINT_STYLE_RUN_UPDATE,
      ENDPOINT_STYLE_RUN_PROGRESS,
      ENDPOINT_STYLE_RUN_EVALUATE,
      ENDPOINT_STYLE_RUN_BLIND_REVIEW,
    ]
    expect(styleRunExtensions.every((endpoint) => endpoint.frozen)).toBe(true)
    expect(ENDPOINT_REGISTRY.filter((e) => !e.frozen)).toHaveLength(0)
  })

  it('训练扩展七端点与 Gate/OpenAPI v5 契约一致（T33-B）', () => {
    for (const e of [
      ENDPOINT_STYLE_RUN_METRICS,
      ENDPOINT_STYLE_RUN_SAMPLES,
      ENDPOINT_STYLE_RUN_SAMPLE_DELETE,
      ENDPOINT_STYLE_RUN_UPDATE,
      ENDPOINT_STYLE_RUN_PROGRESS,
      ENDPOINT_STYLE_RUN_EVALUATE,
      ENDPOINT_STYLE_RUN_BLIND_REVIEW,
    ]) {
      expect(e.frozen).toBe(true)
    }
  })

  it('Candidate API 两端点路径/方法与 OpenAPI v5 契约一致', () => {
    expect(ENDPOINT_TRAINING_SAMPLES).toMatchObject({
      method: 'GET',
      path: '/api/v1/training-samples',
      frozen: true,
    })
    expect(ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW).toMatchObject({
      method: 'POST',
      path: '/api/v1/training-samples/batch-review',
      frozen: true,
    })
  })

  it('CC-4 ocr/extract 与 CC-8 detect-text/extract 均已冻结为 true（B5-02 接线口径）', () => {
    expect(ENDPOINT_OCR_EXTRACT.frozen).toBe(true)
    expect(ENDPOINT_DETECT_TEXT.frozen).toBe(true)
    expect(ENDPOINT_DETECT_TEXT.method).toBe('POST')
    expect(ENDPOINT_DETECT_TEXT.path).toBe('/api/v1/detect-text')
    expect(ENDPOINT_EXTRACT.frozen).toBe(true)
    expect(ENDPOINT_EXTRACT.method).toBe('POST')
    expect(ENDPOINT_EXTRACT.path).toBe('/api/v1/extract')
  })

  it('99 个已登记端点全部 frozen:true', () => {
    const frozenList = ENDPOINT_REGISTRY.filter((e) => e.frozen)
    expect(frozenList).toHaveLength(99)
  })

  it('v0.2.1 五端点齐全且 frozen:true', () => {
    for (const e of [
      ENDPOINT_AUTH_ME,
      ENDPOINT_AUTH_LOGIN,
      ENDPOINT_AUTH_LOGOUT,
      ENDPOINT_AUTH_CHANGE_PASSWORD,
      ENDPOINT_USER_EXPORT,
    ]) {
      expect(e.frozen).toBe(true)
      expect(e.path.startsWith('/api/v1/')).toBe(true)
    }
  })

  it('v0.2 备份五端点补齐（含 detail/verify）且 admin 语义端点存在', () => {
    expect([ENDPOINT_BACKUPS, ENDPOINT_BACKUP_CREATE, ENDPOINT_BACKUP_DETAIL, ENDPOINT_BACKUP_VERIFY, ENDPOINT_BACKUP_RESTORE]).toHaveLength(5)
    expect(ENDPOINT_WORKERS_STATUS.path).toBe('/api/v1/workers/status')
    // v0.4 E-6.5：blessing-templates 别名移除，§1.3 六端点统一新路径
    expect(ENDPOINT_PHRASE_TEMPLATES.path).toBe('/api/v1/phrase-templates')
    expect(ENDPOINT_COVERAGE_CHECK.path).toBe('/api/v1/coverage/check')
  })

  it('全部路径以 /api/v1 起始，方法合法，且 method+path 无重复登记', () => {
    const seen = new Set<string>()
    for (const e of ENDPOINT_REGISTRY) {
      expect(e.path.startsWith('/api/v1/')).toBe(true)
      expect(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']).toContain(e.method)
      const key = `${e.method} ${e.path}`
      expect(seen.has(key)).toBe(false)
      seen.add(key)
    }
  })

  it('fill() 正确替换 {id}/{itemId}/{sampleId} 占位符', () => {
    expect(fill('/api/v1/assets/{id}', { id: 'a1' })).toBe('/api/v1/assets/a1')
    expect(
      fill('/api/v1/render-jobs/{id}/items/{itemId}', { id: 'j1', itemId: 'i9' }),
    ).toBe('/api/v1/render-jobs/j1/items/i9')
    expect(
      fill('/api/v1/style-runs/{id}/samples/{sampleId}', { id: 'r1', sampleId: 's1' }),
    ).toBe('/api/v1/style-runs/r1/samples/s1')
  })
})
