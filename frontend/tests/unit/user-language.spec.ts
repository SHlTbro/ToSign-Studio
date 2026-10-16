import { describe, expect, it } from 'vitest'
import { userError, userText, stateLabel } from '@/utils/userLanguage'
describe('Chinese product messages preserve protocol evidence', () => {
  it('explains actual render qualification reasons without changing protocol identity', () => {
    expect(userText('current artifact does not have layout qualification')).toContain('当前模型尚未完成自然排版资格核验')
    expect(userText('exact positive author/profile artifact identities required')).toContain('缺少已核验的作者、字体或模型身份')
  })
  it('explains stale review and exact missing character count without internal English', () => {
    const raw = '409 REVIEW_JOB_STALE: candidate review implementation changed; Need 20 more distinct eligible characters for M2'
    const text = userText(raw)
    expect(text).toContain('已有字样审核记录已过期'); expect(text).toContain('20 个不同字样通过审核')
    expect(text).toContain('可先重新审核已有字样'); expect(text).not.toContain('补充')
    expect(text).not.toMatch(/REVIEW_JOB_STALE|Need|candidate/)
  })
  it('keeps missing-character guidance when no stale review is reported', () => {
    expect(userText('Need 7 more distinct eligible characters')).toBe('新训练还需补充 7 个不同的合格字符。')
  })
  it('does not change the original API error fields or fabricate success', () => {
    const error = { code: 'E_RUNTIME_BLOCKED', message: 'runtime is not full-model ready' }
    expect(userError(error)).toContain('尚未就绪'); expect(error.code).toBe('E_RUNTIME_BLOCKED')
    expect(stateLabel('UNRECOGNIZED_STATE')).toBe('状态待确认')
    expect(stateLabel('FAIL')).toBe('未通过'); expect(stateLabel('PASS')).toBe('通过')
  })
  it('explains authentication and permission failures without English prefixes', () => {
    expect(userError({ code: 'UNAUTHORIZED', message: '登录已过期' })).toBe('登录已过期，请重新登录。')
    expect(userError({ code: 'FORBIDDEN', message: 'admin role required' })).toContain('需要管理员权限')
    expect(userError({ code: 'NETWORK', message: '网络不可用' })).toBe('网络不可用')
  })
  it('keeps existing Chinese guidance and explains missing evaluation without pretending it ran', () => {
    expect(userText('TIMEOUT：请求超时')).toContain('请求超时')
    expect(userText('unavailable EVAL_NOT_PRODUCED')).toContain('尚未生成效果评估')
    expect(userText('请先上传完整图片')).toBe('请先上传完整图片')
  })
})
