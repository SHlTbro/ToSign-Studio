import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Panel from '@/components/PersonalRuntimeInstallation.vue'
import type { PersonalRuntimeInstallation } from '@/types/domain'
const installation: PersonalRuntimeInstallation = {
  schema: 'windows-personal-style-dependencies/1', status: 'BLOCKED', configuration_status: 'NOT_CONFIGURED',
  interpreter: { key: 'python', status: 'NOT_CONFIGURED', verified: false },
  assets: [{ key: 'font_sans', status: 'BYTES_VERIFIED', verified: true, file: 'NotoSansCJK-Regular.ttc', expected_bytes: 19484784, expected_sha256: 'a'.repeat(64) }],
  full_model: { status: 'NOT_PROBED', configured: false }, execution_policy: { status: 'BLOCKED', code: 'E_RELEASE_BASE_ONLY' },
  qualification: 'NOT_RUN_BY_DIAGNOSTIC', verification_mode: 'METADATA_ONLY', may_prepare: false, may_train: false, may_publish: false,
  recovery: { check_command: "python scripts/windows_personal_runtime_check.py --inputs '<E-drive installation JSON>' --verify-files", steps: [], qualification_entrypoint: 'scripts/windows_scr_training.py', qualification_requires: [], receipt_import_supported: false },
}
describe('P7 installation guidance; no runtime qualification or ML', () => {
  it('shows exact policy, fixed font identity and recovery command without execution controls', () => {
    const wrapper = mount(Panel, { props: { installation } })
    expect(wrapper.text()).toContain('E_RELEASE_BASE_ONLY')
    expect(wrapper.text()).toContain('固定字节已验证')
    expect(wrapper.text()).toContain('19484784 bytes')
    expect(wrapper.text()).toContain('a'.repeat(64))
    expect(wrapper.text()).toContain('不能代替当前源码的真实训练资格证明')
    expect(wrapper.find('code').text()).toBe(installation.recovery.check_command)
    expect(wrapper.findAll('button')).toHaveLength(0)
    expect(wrapper.findAll('input')).toHaveLength(0)
    wrapper.unmount()
  })
  it('replaces stale state on refreshed discovery and stays safe on remount', async () => {
    const wrapper = mount(Panel, { props: { installation } })
    await wrapper.setProps({ installation: { ...installation, configuration_status: 'ACCESS_DENIED', assets: [{ ...installation.assets[0]!, status: 'HASH_MISMATCH', verified: false }] } })
    expect(wrapper.text()).toContain('无法读取')
    expect(wrapper.text()).toContain('SHA256 不匹配')
    expect(wrapper.text()).not.toContain('固定字节已验证')
    expect(wrapper.emitted()).toEqual({})
    wrapper.unmount()
    const restored = mount(Panel, { props: { installation } })
    expect(restored.findAll('button')).toHaveLength(0)
    expect(restored.emitted()).toEqual({})
    restored.unmount()
  })
})
