/**
 * Mock Adapter —— 内存 fixtures（PRD §6 原型规范：mock 数据由端点领域类型单源派生）。
 *
 * 用途：占位壳页面与后续 Gemini SFC 原型消费；不连真实后端。
 * 设计：每个导出函数返回与契约响应 data 同形的对象（已拆信封）。
 * frozen:false 端点（detect-text / extract）返回 fixture，仅供置灰组件调用。
 */
import type {
  Asset,
  Blessing,
  CoverageRow,
  DetectCandidate,
  LayoutTemplate,
  Paged,
  RenderJob,
  RenderItem,
  StyleProfile,
  StyleRun,
  StrokeResult,
  User,
} from '@/types/domain'

// ---- auth ----
export const mockMe: User = {
  user_id: 'a1b2c3d4-0000-4000-8000-000000000001',
  username: 'blogger_x',
  display_name: 'X（博主）',
  role: 'user',
  must_change_password: false,
}

export const mockMeAdmin: User = {
  ...mockMe,
  user_id: 'a1b2c3d4-0000-4000-8000-000000000000',
  username: 'admin',
  display_name: 'Owner',
  role: 'admin',
}

export const mockMeMustChangePwd: User = {
  ...mockMe,
  must_change_password: true,
}

// ---- assets (P2) ----
export const mockAssets: Asset[] = [
  {
    asset_id: 'asset-0001',
    type: 'cos',
    status: 'approved',
    sha256: 'a'.repeat(64),
    bytes: 4_823_112,
    width: 2048,
    height: 2731,
    thumbnail_path: '/media/thumbs/asset-0001.webp',
    created_at: '2026-09-10T08:00:00Z',
    approved_at: '2026-09-10T08:05:00Z',
  },
  {
    asset_id: 'asset-0002',
    type: 'cos',
    status: 'pending_review',
    sha256: 'b'.repeat(64),
    bytes: 6_102_448,
    width: 2048,
    height: 2048,
    thumbnail_path: '/media/thumbs/asset-0002.webp',
    created_at: '2026-09-12T09:30:00Z',
  },
  {
    asset_id: 'asset-0003',
    type: 'sample_source',
    status: 'draft',
    sha256: 'c'.repeat(64),
    bytes: 1_204_880,
    width: 1500,
    height: 1000,
    thumbnail_path: '/media/thumbs/asset-0003.webp',
    created_at: '2026-09-14T19:00:00Z',
  },
  {
    asset_id: 'asset-0004',
    type: 'cos',
    status: 'pending_review',
    sha256: 'd'.repeat(64),
    bytes: 2_398_112,
    width: 1080,
    height: 1920,
    thumbnail_path: '/media/thumbs/asset-0004.webp',
    created_at: '2026-09-15T11:20:00Z',
  },
]

export function mockPagedAssets(page = 1, pageSize = 20): Paged<Asset> {
  return { items: mockAssets, page, page_size: pageSize, total: mockAssets.length }
}

// ---- layout templates (P3) ----
export const mockLayoutTemplates: LayoutTemplate[] = [
  {
    template_id: 'lt-0001',
    name: '图A 标准位',
    version: 1,
    status: 'approved',
    base_asset_id: 'asset-0001',
    canvas_width: 2048,
    canvas_height: 2731,
    slots: [
      {
        key: 'to_cn',
        text_template: 'To {cn}',
        region: { x_norm: 0.62, y_norm: 0.18, w_norm: 0.3, h_norm: 0.12 },
      },
      {
        key: 'blessing',
        text_template: '{blessing}',
        region: { x_norm: 0.1, y_norm: 0.82, w_norm: 0.35, h_norm: 0.08 },
      },
    ],
  },
]

// ---- styles (P6) ----
export const mockStyles: StyleProfile[] = [
  {
    style_id: 1,
    name: '系统默认楷书',
    source_type: 'M0_SYSTEM',
    status: 'approved',
    enabled: true,
    coverage: 1.0,
    model_version: 'system-kai',
    receipt_id: 'sys-m0',
  },
  {
    style_id: 2,
    name: '冻结手写体 SDT',
    source_type: 'M1_FROZEN',
    status: 'approved',
    enabled: true,
    coverage: 0.92,
    model_version: 'sdt-v1',
    receipt_id: 'sys-m1',
  },
  {
    style_id: 3,
    name: '我的手写样本集',
    source_type: 'HANDWRITING_ASSET',
    status: 'approved',
    enabled: true,
    coverage: 0.71,
    model_version: 'hw-01',
    receipt_id: 'rcpt-0001',
  },
  {
    style_id: 4,
    name: 'FontDiffuser LoRA (Beta)',
    source_type: 'M2_ADAPTER',
    status: 'pending_review',
    enabled: false,
    coverage: 0.6,
    model_version: 'lora-beta',
    receipt_id: 'rcpt-m2',
  },
]

// ---- style runs (P7) ----
export const mockStyleRuns: StyleRun[] = [
  {
    run_id: 1,
    status: 'running',
    sample_ids: [],
    config: { base: 'font_chain_v1', epochs: 100, lr: 0.0001 },
    created_at: '2026-09-28T00:00:00Z',
    updated_at: '2026-09-28T00:00:00Z',
  },
]

// ---- blessing templates (P8) ----
export const mockBlessings: Blessing[] = [
  { id: 'b-0001', text: '天天开心', enabled: true, created_by: 'blogger_x', updated_at: '2026-09-11T10:00:00Z' },
  { id: 'b-0002', text: '万事胜意', enabled: true, created_by: 'blogger_x', updated_at: '2026-09-11T10:00:00Z' },
  { id: 'b-0003', text: '暴富暴美', enabled: false, created_by: 'blogger_x', updated_at: '2026-09-12T10:00:00Z' },
]

// ---- blogger signature presets (P8 模版语库 · 博主署名) ----
// 提案类型（待契约化）：博主名可变可预设，P5 逐行可选择是否署名
export interface BloggerNamePreset {
  id: string
  name: string
  is_default: boolean
  updated_at: string
}
export const mockBloggerNames: BloggerNamePreset[] = [
  { id: 'bn-0001', name: '星河', is_default: true, updated_at: '2026-09-10T10:00:00Z' },
  { id: 'bn-0002', name: '星野手作', is_default: false, updated_at: '2026-09-12T10:00:00Z' },
]

// ---- training set images (P7 训练控制台) ----
// 提案类型（待契约化）：to 签 cos 训练图，清洗后进入训练集
export interface TrainingImage {
  image_id: string
  file_name: string
  width: number
  height: number
  size_kb: number
  status: 'uploaded' | 'cleaning' | 'ready'
  uploaded_at: string
}
export const mockTrainingImages: TrainingImage[] = [
  { image_id: 'ti-0001', file_name: 'IMG_2031.jpg', width: 1080, height: 1920, size_kb: 2840, status: 'ready', uploaded_at: '2026-09-13T09:00:00Z' },
  { image_id: 'ti-0002', file_name: 'IMG_2032.jpg', width: 1080, height: 1920, size_kb: 2610, status: 'ready', uploaded_at: '2026-09-13T09:01:00Z' },
  { image_id: 'ti-0003', file_name: 'IMG_2045.jpg', width: 1440, height: 1920, size_kb: 3120, status: 'cleaning', uploaded_at: '2026-09-14T15:20:00Z' },
  { image_id: 'ti-0004', file_name: 'IMG_2050.jpg', width: 1080, height: 1920, size_kb: 2490, status: 'uploaded', uploaded_at: '2026-09-15T08:40:00Z' },
]

// ---- style presets (P6 新建手写资产 · 编排者预设基底字库) ----
// 提案类型（待契约化）：预设基底字体，下载后作为 LoRA 训练基底或直接启用
export interface StylePreset {
  preset_id: string
  name: string
  description: string
  font_stack: string
  file_size: string
  version: string
}
export const mockStylePresets: StylePreset[] = [
  { preset_id: 'preset-kai', name: '楷书基底', description: '端正平稳，适配正式长文祝福', font_stack: '"Kaiti SC","STKaiti","KaiTi",serif', file_size: '8.2 MB', version: 'v1.3' },
  { preset_id: 'preset-xing', name: '行书基底', description: '连笔流畅，适合短句 To 签', font_stack: '"STXingkai","Xingkai SC","Segoe Script",cursive', file_size: '7.6 MB', version: 'v1.1' },
  { preset_id: 'preset-yuan', name: '圆体基底', description: '软糯亲和，少女感配色图首选', font_stack: '"Yuanti SC","YouYuan","Segoe Print",cursive', file_size: '9.0 MB', version: 'v2.0' },
  { preset_id: 'preset-hei', name: '黑体基底', description: '几何利落，海报感排版适配', font_stack: '"Heiti SC","SimHei","Microsoft YaHei",sans-serif', file_size: '6.8 MB', version: 'v1.0' },
]

// ---- render jobs / items (P5) ----
export const mockRenderJobs: RenderJob[] = [
  {
    job_id: 'job-0001',
    status: 'pending_review',
    draft: false,
    item_count: 12,
    created_at: '2026-09-14T20:00:00Z',
  },
]

export const mockRenderItems: RenderItem[] = [
  {
    item_id: 'item-0001',
    status: 'completed',
    fallback_used: false,
    fallback_map: {},
    warnings: [],
    retryable: false,
    render_version: 1,
  },
  {
    item_id: 'item-0002',
    status: 'completed',
    fallback_used: true,
    fallback_map: { '彧': 'simkai' },
    warnings: ['生僻字「彧」走字体链回退'],
    retryable: false,
    render_version: 1,
  },
]

export const mockCoverageRows: CoverageRow[] = [
  { text: '小美', missing: [], fallback: {}, risk: 'low' },
  { text: '阿强', missing: [], fallback: {}, risk: 'low' },
  { text: '彧', missing: ['彧'], fallback: { '彧': 'simkai' }, risk: 'high' },
]

// ---- detect-text / extract (P4, frozen:false) ----
export const mockDetectCandidates: DetectCandidate[] = [
  {
    id: 'c1',
    box_norm: { x_norm: 0.62, y_norm: 0.18, w_norm: 0.3, h_norm: 0.12 },
    text: '小美',
    confidence: 0.87,
  },
]

export const mockStrokeResults: StrokeResult[] = [
  {
    ref: 'c1',
    stroke_path: 'users/a1b2c3d4/strokes/sample-0001.png',
    width: 512,
    height: 384,
    quality: 'ok',
  },
]
