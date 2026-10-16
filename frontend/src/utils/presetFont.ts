import { request } from '@/api/http'

// The ticket endpoint requires the signed-in user; the returned URL lets FontFace
// fetch the same TTF that the renderer uses without custom request headers.
const loadedFamilies = new Map<string, string>()
const pendingLoads = new Map<string, Promise<string>>()
let nextStyleFamilyId = 0

function familyFor(fontUrl: string): string {
  const presetId = fontUrl.match(/\/style-presets\/([^/]+)\/download$/)?.[1]
  return presetId
    ? `yy-preset-${presetId.replace(/[^a-zA-Z0-9_-]/g, '')}`
    : `yy-style-${++nextStyleFamilyId}`
}

export function loadPresetFont(fontUrl: string): Promise<string> {
  const loaded = loadedFamilies.get(fontUrl)
  if (loaded) return Promise.resolve(loaded)
  const pending = pendingLoads.get(fontUrl)
  if (pending) return pending

  const task = (async () => {
    if (typeof FontFace === 'undefined' || !document.fonts) {
      throw new Error('此浏览器无法预览字体')
    }
    const family = familyFor(fontUrl)
    const ticket = await request<{ url?: string }>(fontUrl)
    const src = ticket?.url
    if (typeof src !== 'string' || !src || /["'\\\n\r]/.test(src)) {
      throw new Error('字体下载地址无效')
    }
    const face = new FontFace(family, `url("${src}")`)
    await face.load()
    document.fonts.add(face)
    loadedFamilies.set(fontUrl, family)
    return family
  })()

  pendingLoads.set(fontUrl, task)
  void task.then(
    () => pendingLoads.delete(fontUrl),
    () => pendingLoads.delete(fontUrl),
  )
  return task
}
