<script setup>
import { ref, onMounted } from 'vue'
import { api, fileToB64 } from '../lib/api.js'

const s = ref(null)
const enabled = ref(false)
const track = ref('')
const volume = ref(0.18)
const duckingEnabled = ref(false)
const duckingStrength = ref('balanced')
const msg = ref('')
const umsg = ref('')
const fileInput = ref(null)

async function load() {
  try {
    s.value = await api('GET', '/api/bgm')
    enabled.value = !!s.value.enabled
    track.value = s.value.track || ''
    volume.value = s.value.volume
    duckingEnabled.value = !!s.value.ducking_enabled
    duckingStrength.value = s.value.ducking_strength || 'balanced'
  } catch (e) { msg.value = '加载失败：' + e.message }
}
onMounted(load)

async function save() {
  try {
    await api('PUT', '/api/bgm', {
      enabled: enabled.value,
      track: track.value || null,
      volume: parseFloat(volume.value) || 0,
      ducking_enabled: duckingEnabled.value,
      ducking_strength: duckingStrength.value,
    })
    msg.value = '已保存 ✓'
  }
  catch (e) { msg.value = '失败：' + e.message }
}

async function upload() {
  const f = fileInput.value?.files?.[0]
  if (!f) { umsg.value = '请先选择文件'; return }
  umsg.value = '上传中…'
  try { await api('POST', '/api/bgm/upload', { filename: f.name, data_b64: await fileToB64(f) }); umsg.value = '已上传 ✓'; load() }
  catch (e) { umsg.value = '失败：' + e.message }
}
</script>

<template>
  <div>
    <div v-if="!s" class="muted">加载中…</div>
    <template v-else>
      <label><input type="checkbox" v-model="enabled" /> 启用背景音乐</label>
      <label>曲目</label>
      <select v-model="track">
        <option value="">（不使用）</option>
        <option v-for="t in (s.tracks || [])" :key="t" :value="t">{{ t }}</option>
      </select>
      <label>音量（0–1）<span class="help" data-tip="背景音乐相对成片的音量。0=静音，1=原始音量；有人声配音时建议 0.1–0.25，避免盖过配音；纯音乐片可调高">?</span></label>
      <input type="number" step="0.05" min="0" max="1" v-model="volume" />
      <label><input type="checkbox" v-model="duckingEnabled" /> 对白时自动降低音乐</label>
      <label>压低强度</label>
      <select v-model="duckingStrength" :disabled="!duckingEnabled">
        <option value="gentle">轻柔</option>
        <option value="balanced">标准</option>
        <option value="strong">明显</option>
      </select>
      <div style="margin-top:10px"><button class="act" @click="save">保存</button> <span class="muted">{{ msg }}</span></div>
      <hr style="border-color:var(--line);margin:14px 0" />
      <label>上传自定义音乐（mp3/wav…）</label>
      <input ref="fileInput" type="file" accept="audio/*" />
      <div style="margin-top:8px"><button class="ghost" @click="upload">上传到曲库</button> <span class="muted">{{ umsg }}</span></div>
    </template>
  </div>
</template>
