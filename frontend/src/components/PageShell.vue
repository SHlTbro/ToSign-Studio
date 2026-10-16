<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
  gated?: boolean
}>()
</script>

<template>
  <main class="flex h-[100dvh] flex-col items-center justify-center gap-3 p-6">
    <h1 class="text-2xl font-bold" data-test="page-title">{{ title }}</h1>
    <p v-if="subtitle" class="text-center text-sm" data-test="page-subtitle">{{ subtitle }}</p>
    <span
      v-if="gated"
      class="rounded-full border-2 border-dashed border-[var(--color-amber)] px-3 py-1 text-xs"
      data-test="frozen-badge"
    >
      frozen:false · 置灰占位
    </span>
    <nav class="mt-4 flex flex-wrap items-center justify-center gap-2">
      <RouterLink
        to="/"
        class="tbtn inline-flex items-center bg-[var(--color-surface)] px-4 text-sm"
      >
        ← 工作台
      </RouterLink>
    </nav>
  </main>
</template>
