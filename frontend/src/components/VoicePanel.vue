<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api.js'

const s = ref(null)
const provider = ref('')
const model = ref('')
const voice = ref('')
const enabled = ref(false)
const msg = ref('')
const audio = ref(null)
const audioSrc = ref('')

const cat = computed(() => (s.value && s.value.catalog) || {})
const models = computed(() => (cat.value[provider.value] || {}).models || [])
const voices = computed(() => (cat.value[provider.value] || {}).voices || [])

onMounted(async () => {
  try {
    s.value = await api('GET', '/api/voice')
    enabled.value = !!s.value.enabled
    provider.value = s.value.provider || Object.keys(cat.value)[0] || ''
    model.value = s.value.model || (models.value[0] || '')
    voice.value = s.value.voice || (voices.value[0] && voices.value[0].id) || ''
  } catch (e) { msg.value = '加载失败：' + e.message }
})

function onProvider() {
  model.value = models.value[0] || ''
  voice.value = (voices.value[0] && voices.value[0].id) || ''
}

async function save() {
  try { await api('PUT', '/api/voice', { enabled: enabled.value, provider: provider.value, model: model.value, voice: voice.value }); msg.value = '已保存 ✓' }
  catch (e) { msg.value = '失败：' + e.message }
}

async function preview() {
  msg.value = '合成中…'
  try {
    const r = await api('POST', '/api/voice/preview', { provider: provider.value, model: model.value, voice: voice.value })
    audioSrc.value = 'data:audio/' + (r.format || 'mp3') + ';base64,' + r.audio_b64
    msg.value = ''
    setTimeout(() => audio.value && audio.value.play(), 50)
  } catch (e) { msg.value = '试听失败：' + e.message }
}
</script>

<template>
  <div>
    <div v-if="!s" class="muted">加载中…</div>
    <template v-else>
      <label><input type="checkbox" v-model="enabled" /> 启用配音</label>
      <label>提供方</label>
      <select v-model="provider" @change="onProvider">
        <option v-for="(v, p) in cat" :key="p" :value="p">{{ v.label || p }}</option>
      </select>
      <label>模型</label>
      <select v-model="model"><option v-for="m in models" :key="m" :value="m">{{ m }}</option></select>
      <label>音色</label>
      <select v-model="voice"><option v-for="v in voices" :key="v.id" :value="v.id">{{ v.label }}</option></select>
      <div style="margin-top:10px">
        <button class="ghost" @click="preview">▶ 试听</button>
        <button class="act" @click="save">保存</button>
        <span class="muted">{{ msg }}</span>
      </div>
      <audio v-if="audioSrc" ref="audio" :src="audioSrc" controls style="margin-top:10px;width:100%"></audio>
    </template>
  </div>
</template>
