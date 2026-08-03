<script setup>
import { ref, onMounted } from 'vue'
import { api, fileToB64 } from '../lib/api.js'

const s = ref(null)
const enabled = ref(false)
const volume = ref(0.5)
const msg = ref('')
const umsg = ref('')
const fileInput = ref(null)

async function load() {
  try {
    s.value = await api('GET', '/api/sfx')
    enabled.value = !!s.value.enabled
    volume.value = s.value.volume
  } catch (e) { msg.value = '加载失败：' + e.message }
}
onMounted(load)

async function save() {
  try { await api('PUT', '/api/sfx', { enabled: enabled.value, volume: parseFloat(volume.value) || 0 }); msg.value = '已保存 ✓' }
  catch (e) { msg.value = '失败：' + e.message }
}

async function upload() {
  const f = fileInput.value?.files?.[0]
  if (!f) { umsg.value = '请先选择文件'; return }
  umsg.value = '上传中…'
  try { await api('POST', '/api/sfx/upload', { filename: f.name, data_b64: await fileToB64(f) }); umsg.value = '已上传 ✓'; load() }
  catch (e) { umsg.value = '失败：' + e.message }
}
</script>

<template>
  <div>
    <div v-if="!s" class="muted">加载中…</div>
    <template v-else>
      <label><input type="checkbox" v-model="enabled" /> 启用音效</label>
      <label>音量（0–1）<span class="help" data-tip="音效相对成片的音量。0=静音，1=原始音量；通常 0.3–0.6，叠在配音/BGM 之上不至于太突兀">?</span></label>
      <input type="number" step="0.05" min="0" max="1" v-model="volume" />
      <div style="margin-top:10px"><button class="act" @click="save">保存</button> <span class="muted">{{ msg }}</span></div>
      <hr style="margin:14px 0" />
      <label>已有音效</label>
      <div style="margin:4px 0">
        <span v-for="f in (s.files || [])" :key="f" class="tag" style="margin:2px">{{ f }}</span>
        <span v-if="!(s.files || []).length" class="muted">（曲库为空）</span>
      </div>
      <label>上传音效（按关键词命名）</label>
      <input ref="fileInput" type="file" accept="audio/*" />
      <div style="margin-top:8px"><button class="ghost" @click="upload">上传到音效库</button> <span class="muted">{{ umsg }}</span></div>
    </template>
  </div>
</template>
