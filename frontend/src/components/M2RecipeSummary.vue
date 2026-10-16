<script setup lang="ts">
import { computed } from 'vue'
import { record } from '@/api/m2Runtime'
import { isUserScrRecipe } from '@/api/userScrRecipe'
const props = defineProps<{ config: Record<string, unknown>; frozen?: boolean }>()
const budget = computed(() => record(props.config.training_budget))
const train = computed(() => record(props.config.train))
function value(input: unknown): string { return typeof input === 'string' || (typeof input === 'number' && Number.isFinite(input)) ? String(input) : '未记录' }
</script>

<template>
  <div class="mt-2 space-y-1 rounded-lg border border-ink/20 p-2 text-[11px]" :data-testid="frozen ? 'm2-frozen-recipe' : 'm2-server-recipe'">
    <h4 class="font-black">{{ frozen ? '当前运行冻结配方' : '新运行服务端配方' }}</h4>
    <p v-if="isUserScrRecipe(config.recipe_id)">配方与训练步数由服务端锁定；时间上限可在新建时设置或启动前保存，以重新读取的运行配置为准。</p>
    <p v-else>配方由服务端锁定，M2 不提供可覆盖这些参数的滑块或输入框。</p>
    <p class="break-all">配方：{{ value(config.recipe_id) }} · 目标：{{ value(config.objective_profile) }}</p>
    <p>设备：{{ value(train?.device) }} · 完整 epoch：{{ value(budget?.expected_complete_epochs) }} · 优化器步数：{{ value(budget?.expected_optimizer_steps) }}</p>
    <p>已验证训练样本：{{ value(budget?.verified_train_count) }} · batch：{{ value(budget?.batch) }} · 每 epoch 步数：{{ value(budget?.steps_per_epoch) }}</p>
    <p>时间预算：{{ value(train?.max_seconds) }} 秒 · 检查点：{{ value(config.checkpoint_selection) }}</p>
    <p class="break-all">预处理：{{ value(config.preprocessing_profile) }}</p>
    <p v-if="budget?.terminal_validation_required === true">必须完成全部 epoch、步数及末态验证；以上是训练预算，不是已完成进度，也不代表质量通过。</p>
    <p v-if="!budget" class="text-amber">未记录完整配方或预算；不会用当前默认值补写历史运行，启动仍须服务端校验。</p>
    <p v-if="frozen">切换新运行环境不会修改此运行的配方或绑定。</p>
  </div>
</template>
