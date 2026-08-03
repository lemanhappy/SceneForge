<script setup>
import { onMounted, onUnmounted } from 'vue'
import { lightboxState, closeLightbox } from '../lib/lightbox.js'
function onKey(e) { if (e.key === 'Escape') closeLightbox() }
onMounted(() => document.addEventListener('keydown', onKey))
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <div v-if="lightboxState.src" class="lightbox" @click="closeLightbox">
    <img :src="lightboxState.src" />
    <div class="hint">点击 / Esc 关闭</div>
  </div>
</template>
