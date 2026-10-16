/**
 * GWT：Given frozen:false 标记，When 非置灰组件 import（测试桩），Then lint 报错。
 *
 * 用 ESLint Linter（flat config）对内存代码桩做校验，加载工程 eslint.config.js 中
 * 承载 frozen-gate 规则的配置块（与 `pnpm lint` 同一规则实现）：
 *  - 无 frozen-gated 标记的文件 import/引用 ENDPOINT_DETECT_TEXT|ENDPOINT_EXTRACT → error
 *  - 含 frozen-gated:true 块注释的置灰组件 → 放行
 */
import { Linter } from 'eslint'
import { beforeAll, describe, expect, it } from 'vitest'

const RULE_ID = 'frozen-gate/no-frozen-endpoint-outside-gated'

let linter: Linter
let gateConfig: unknown[]

beforeAll(async () => {
  // 动态导入工程 flat config（避免给 vue-tsc 引入 .js 声明依赖）
  // @ts-expect-error eslint.config.js 为无类型 JS 配置，运行期由 ESLint 自行解析
  const mod = await import('../../eslint.config.js')
  const fullConfig = mod.default as Record<string, unknown>[]
  // 仅取承载 frozen-gate 插件与规则的配置块（与 pnpm lint 同一份规则实现）
  const gateBlock = fullConfig.find(
    (c) => c.rules && Object.prototype.hasOwnProperty.call(c.rules as object, RULE_ID),
  )
  if (!gateBlock) throw new Error('eslint.config.js 中未找到 frozen-gate 规则块')
  gateConfig = [gateBlock]
  linter = new Linter({ configType: 'flat' })
}, 30_000)

function lint(code: string) {
  // flat Linter 的 config 必须作为 verify 第二参数传入；第三参为文件名字符串
  return linter.verify(code, gateConfig as never, 'src/pages/Stub.js')
}

const BAD_DETECT = `
import { ENDPOINT_DETECT_TEXT } from '@/api/endpoints'
export function x() { return ENDPOINT_DETECT_TEXT.path }
`

const BAD_EXTRACT = `
import { ENDPOINT_EXTRACT } from '@/api/endpoints'
console.log(ENDPOINT_EXTRACT)
`

const GOOD_GATED = `
/* frozen-gated: true */
import { ENDPOINT_DETECT_TEXT, ENDPOINT_EXTRACT } from '@/api/endpoints'
void ENDPOINT_DETECT_TEXT
void ENDPOINT_EXTRACT
`

describe('frozen-gate lint：frozen:false 仅置灰组件可消费', () => {
  it('非置灰文件 import ENDPOINT_DETECT_TEXT → lint 报错', () => {
    const messages = lint(BAD_DETECT)
    expect(messages.some((m) => m.ruleId === RULE_ID)).toBe(true)
  })

  it('非置灰文件引用 ENDPOINT_EXTRACT → lint 报错', () => {
    const messages = lint(BAD_EXTRACT)
    expect(messages.some((m) => m.ruleId === RULE_ID)).toBe(true)
  })

  it('含 frozen-gated:true 块注释的置灰组件 → 零 frozen-gate 报错', () => {
    const messages = lint(GOOD_GATED)
    expect(messages.filter((m) => m.ruleId === RULE_ID)).toHaveLength(0)
  })
})
