<script setup lang="ts">
import type { PersonalRuntimeInstallation } from '@/types/domain'
defineProps<{ installation: PersonalRuntimeInstallation }>()
const statuses: Record<string, string> = {
  NOT_CONFIGURED: '未配置', MISSING: '文件缺失', ACCESS_DENIED: '无法读取', INVALID: '配置无效',
  INVALID_PATH: '路径无效', PRESENT: '配置已找到', PRESENT_UNVERIFIED: '文件存在，尚未验证',
  SIZE_MISMATCH: '字节数不匹配', HASH_MISMATCH: 'SHA256 不匹配', BYTES_VERIFIED: '固定字节已验证',
  NOT_PROBED: '尚未检查',
}
const labels: Record<string, string> = {
  module: 'SCR 特征模块', checkpoint: 'SCR 特征权重', state_schema: '权重结构',
  font_sans: 'Noto Sans 固定字体', font_serif: 'Noto Serif 固定字体', source_ledger: '真实图片来源台账',
}
</script>

<template>
  <section class="mt-3 min-w-0 rounded-lg border border-amber p-3 text-xs" data-testid="personal-runtime-installation" aria-label="个人风格训练环境安装状态">
    <p class="font-black">个人风格训练环境尚未完成真实资格验收</p>
    <p class="mt-1">Base 安装提供图片审核和标注。启动真实 M2 还需要独立安装并通过验收的个人风格训练环境。</p>
    <p class="mt-1 break-all" role="status">执行门：{{ installation.execution_policy.code }}；安装配置：{{ statuses[installation.configuration_status] ?? installation.configuration_status }}</p>
    <ul class="mt-2 space-y-1">
      <li>ML Python：{{ statuses[installation.interpreter.status] ?? installation.interpreter.status }}</li>
      <li>完整模型：{{ statuses[installation.full_model.status] ?? installation.full_model.status }}</li>
      <li v-for="asset in installation.assets" :key="asset.key">{{ labels[asset.key] ?? asset.key }}：{{ statuses[asset.status] ?? asset.status }}</li>
    </ul>
    <p class="mt-2">刷新环境会重新读取服务器状态。文件存在或字体校验通过，均不能代替当前源码的真实训练资格证明。</p>
    <details class="mt-2">
      <summary class="cursor-pointer font-bold">安装与恢复步骤</summary>
      <ol class="mt-2 list-inside list-decimal space-y-1">
        <li>在 E 盘安装 ML Python、完整模型、固定 SCR 特征文件和两份指定字体，由本机管理员配置路径。</li>
        <li>运行只读依赖检查；遇到访问被拒绝时恢复正规安装，不修改权限或占用其他任务的训练资源。</li>
        <li>使用独立个人风格运行包及当前源码的正式 Windows CPU producer 完成真实资格验收。</li>
        <li>刷新 P7，选择服务器验证通过的登记，准备本人冻结 S03，再创建并启动 M2。</li>
      </ol>
      <p class="mt-2">本机管理员检查命令（请替换配置路径）：</p>
      <code class="mt-1 block whitespace-pre-wrap break-all">{{ installation.recovery.check_command }}</code>
      <p class="mt-2">检查不启动训练、不写入资格凭据。模型质量、评估和发布门仍需逐项通过。</p>
      <ul class="mt-2 space-y-2">
        <li v-for="asset in installation.assets.filter(item => item.expected_sha256)" :key="asset.key" class="break-all text-[10px]">
          {{ asset.file ?? labels[asset.key] ?? asset.key }}<span v-if="asset.expected_bytes"> · {{ asset.expected_bytes }} bytes</span><br>SHA256 {{ asset.expected_sha256 }}
        </li>
      </ul>
    </details>
  </section>
</template>
