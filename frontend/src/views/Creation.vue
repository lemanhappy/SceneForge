<script setup>
import { ref, watch } from 'vue'
import { BookOpen, Clapperboard } from '@lucide/vue'
import Production from './Production.vue'
import Series from './Series.vue'

const props = defineProps({
  resetKey: { type: Number, default: 0 },
})
const emit = defineEmits(['sessions-changed'])
const mode = ref('single')

watch(() => props.resetKey, () => {
  mode.value = 'single'
})
</script>

<template>
  <div class="creation-workspace">
    <div class="creation-mode-bar">
      <div class="creation-mode-tabs" role="tablist" aria-label="创作类型">
        <button type="button" role="tab" :aria-selected="mode === 'single'" :class="{ active: mode === 'single' }" @click="mode = 'single'">
          <Clapperboard :size="16" />单条视频
        </button>
        <button type="button" role="tab" :aria-selected="mode === 'series'" :class="{ active: mode === 'series' }" @click="mode = 'series'">
          <BookOpen :size="16" />连续短剧
        </button>
      </div>
    </div>

    <keep-alive>
      <Production v-if="mode === 'single'" :reset-key="resetKey" @sessions-changed="emit('sessions-changed')" />
      <Series v-else @sessions-changed="emit('sessions-changed')" />
    </keep-alive>
  </div>
</template>
