import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P7Training from '@/pages/P7_Training.vue'
import { envelope, flushPromises, mountPage } from './_harness'

beforeEach(() => { localStorage.clear() })
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('训练参考图独立入口', () => {
  it('guides extraction from original To签 references without a direct cropped-training-image upload control', async () => {
    const { wrapper, logs } = mountPage(P7Training, { plan: {
      '/api/v1/styles': envelope({ items: [{ style_id: 1, name: 'Owned reference style', source_type: 'HANDWRITING_ASSET', status: 'approved' }] }),
      '/api/v1/styles/1/samples': envelope({ items: [{ sample_id: 70, text_label: '字', source_training_image_id: 70, touchup_confirmed: true }] }),
      '/api/v1/styles/1/m2-runtime': envelope({ ready: false, blockers: [], capabilities: {} }),
      '/api/v1/training-sets': envelope({ items: [{ image_id: 70, file_name: 'original-preserved-crop.png', status: 'uploaded', width: 128, height: 128, size_bytes: 1024 }], total: 1, page: 1, page_size: 24 }),
      '/api/v1/assets': envelope({ items: [{ asset_id: 'source-one', type: 'sample_source', status: 'approved' }] }, { total: 1, page: 1, page_size: 20 }),
    } })
    await flushPromises()
    const step = wrapper.get('[data-testid="training-step-1"]')
    expect(step.text()).toContain('从 To 签参考图提取字形 → 确认字样 → 生成训练集')
    expect(step.find('input[type="file"]').exists()).toBe(false)
    expect(step.findAll('button').some(button => button.text().includes('多选上传'))).toBe(false)
    expect(step.text()).toContain('已登记')
    expect(step.text()).toContain('1 条已登记字形 · 1 个已登记字符')
    expect(step.text()).toContain('训练资格以正式审核为准')
    expect(step.text()).not.toContain('有效字形')
    const crop = step.get('[data-testid="existing-training-glyph"]')
    expect((crop.element as HTMLElement).style.backgroundColor).toBe('rgb(170, 170, 170)')
    expect(step.get('[data-testid="historical-training-materials"]').attributes('open')).toBeUndefined()
    expect(step.text()).toContain('登记不表示可训练')
    expect(crop.find('details').text()).toContain('original-preserved-crop.png')
    const references = step.get('[data-testid="training-reference-images"]')
    expect(references.find('a').attributes('href')).toBe('/extract/source-one?style_id=1')
    expect(references.find('details').text()).toContain('source-one')
    expect(references.text()).toContain('参考图 1')
    expect(logs.filter(log => log.method !== 'GET')).toHaveLength(0)
    wrapper.unmount()
  })
  it('训练页展示 sample_source 原图，保留资产 ID 并链接到正式提取页', async () => {
    const source = { asset_id: 'reference-1', type: 'sample_source', status: 'approved',
      sha256: 'a'.repeat(64), bytes: 1071876, width: 748, height: 1135,
      thumbnail_path: '1/thumbnails/reference-1.png', created_at: '2026-10-07T00:00:00Z' }
    const { wrapper, logs } = mountPage(P7Training, { plan: {
      '/api/v1/styles/unpublished-models': envelope({ items: [], total: 0, page: 1, page_size: 20, registry_status: 'not_installed' }),
      '/api/v1/styles': envelope({ items: [] }),
      '/api/v1/training-sets': envelope({ items: [] }, { total: 0, page: 1, page_size: 24 }),
      '/api/v1/assets': envelope({ items: [source] }, { total: 1, page: 1, page_size: 20 }),
    } })
    await flushPromises()
    expect(logs.some(log => log.url.includes('/api/v1/assets?type=sample_source&'))).toBe(true)
    const area = wrapper.find('[data-testid="training-reference-images"]')
    expect(area.exists()).toBe(true)
    expect(area.text()).toContain('训练参考图')
    expect(area.text()).toContain('可含 To签')
    expect(area.find('img').attributes('src')).toBe('/api/v1/assets/reference-1/thumbnail')
    expect(area.find('a').attributes('href')).toBe('/extract/reference-1')
    expect(logs.filter(log => log.method !== 'GET')).toHaveLength(0)
  })
})
