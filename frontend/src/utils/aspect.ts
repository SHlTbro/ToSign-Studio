/**
 * 图片比例适配（v0.7 前后端约定，详见 docs/aspect-ratio-contract.md）
 *
 * 铁律：任何展示/标注容器都不得拉伸原图，统一按 Asset.width/height 等比 contain。
 * Region 归一化坐标 [0,1] 始终相对“该 asset 自身宽高”，故坐标计算与像素比例解耦，
 * 只需让坐标系容器的 CSS aspect-ratio 与原图一致即可。
 */

/** 支持的宽高比闭区间（超出仍 contain 居中，只是视为异常规格） */
export const ASPECT_MIN = 1 / 3
export const ASPECT_MAX = 3
/** 元数据缺失/非法时的兜底比例（竖版 3:4，仅占位，不代表真实图） */
export const FALLBACK_ASPECT = 3 / 4

export type Orientation = 'portrait' | 'square' | 'landscape'

function toPositiveInt(v: unknown): number | null {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 && Math.floor(n) === n ? n : null
}

/** 返回可直接绑给 CSS aspect-ratio 的字符串；非法输入回退 3/4 */
export function aspectRatioValue(width?: number | null, height?: number | null): string {
  const w = toPositiveInt(width)
  const h = toPositiveInt(height)
  if (!w || !h) return `${FALLBACK_ASPECT}`
  // 约分让浏览器拿到最简比例，避免浮点尾巴
  const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b))
  const g = gcd(w, h)
  return `${w / g} / ${h / g}`
}

/** 数值比例（宽/高） */
export function aspectRatioNumber(width?: number | null, height?: number | null): number {
  const w = toPositiveInt(width)
  const h = toPositiveInt(height)
  if (!w || !h) return FALLBACK_ASPECT
  return w / h
}

export function orientationOf(width?: number | null, height?: number | null): Orientation {
  const r = aspectRatioNumber(width, height)
  if (Math.abs(r - 1) < 0.02) return 'square'
  return r > 1 ? 'landscape' : 'portrait'
}

/** 是否落在常规支持区间 1:3 ~ 3:1 */
export function isSupportedAspect(width?: number | null, height?: number | null): boolean {
  const r = aspectRatioNumber(width, height)
  return r >= ASPECT_MIN && r <= ASPECT_MAX
}
