<script setup>
import { computed } from 'vue'
import { jobProgress, shotGrid } from '../lib/progress.js'

const props = defineProps({ progress: { type: Array, default: () => [] }, compact: { type: Boolean, default: false } })
const p = computed(() => jobProgress(props.progress))
const pct = computed(() => (p.value.frac == null ? null : Math.round(p.value.frac * 100)))
const grid = computed(() => (props.compact ? null : shotGrid(props.progress)))
</script>

<template>
  <div class="jobprog">
    <div class="jp-top">
      <span>{{ p.label }}</span>
      <span class="muted">{{ pct == null ? ('已 ' + p.steps + ' 步') : (pct + '%') }}</span>
    </div>
    <div class="jp-bar">
      <div v-if="pct == null" class="jp-fill jp-indet"></div>
      <div v-else class="jp-fill" :style="{ width: pct + '%' }"></div>
    </div>
    <template v-if="grid">
      <div class="shot-legend muted">关键帧 {{ grid.frames }}/{{ grid.total }} · 视频 {{ grid.vids }}/{{ grid.total }}</div>
      <div class="shotgrid">
        <div v-for="(c, i) in grid.cells" :key="i" class="shotcell" :class="c.cls" :title="c.tip">{{ c.label }}</div>
      </div>
    </template>
  </div>
</template>
