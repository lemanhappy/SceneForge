<script setup>
import { onMounted, onUnmounted } from 'vue'
import { confirmState, resolveConfirm } from '../lib/confirm.js'

function onKey(e) {
  if (!confirmState.open) return
  if (e.key === 'Escape') resolveConfirm(false)
  else if (e.key === 'Enter') resolveConfirm(true)
}
onMounted(() => document.addEventListener('keydown', onKey))
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <div v-if="confirmState.open" class="modal-mask" @click.self="resolveConfirm(false)">
    <div class="modal-box">
      <div class="modal-msg">{{ confirmState.message }}</div>
      <div class="modal-btns">
        <button class="ghost" @click="resolveConfirm(false)">{{ confirmState.cancelText }}</button>
        <button class="act" :class="{ 'modal-danger': confirmState.danger }" @click="resolveConfirm(true)">{{ confirmState.okText }}</button>
      </div>
    </div>
  </div>
</template>
