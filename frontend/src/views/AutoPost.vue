<script setup>
import { ref, onMounted } from 'vue'
import { api, mediaUrl } from '../lib/api.js'

const videos = ref([])
const sel = ref('')
const fileInput = ref(null)
const umsg = ref('')
const asrmsg = ref('')
const lines = ref('')
const subs = ref(true)
const vo = ref(false)
const replace = ref(true)
const bgm = ref('')
const label = ref(true)
const msg = ref('')
const outUrl = ref('')

async function load() {
  try {
    const s = await api('GET', '/api/edit')
    videos.value = s.videos || []
    if (!sel.value && videos.value.length) sel.value = videos.value[0]
  } catch (e) { /* ignore */ }
}
onMounted(load)

async function upload() {
  const f = fileInput.value?.files?.[0]
  if (!f) { umsg.value = '请选择文件'; return }
  umsg.value = '上传中…（大文件较慢）'
  try {
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader()
      r.onload = () => res(String(r.result).split(',')[1])
      r.onerror = rej
      r.readAsDataURL(f)
    })
    await api('POST', '/api/edit/upload', { filename: f.name, data_b64: b64 })
    await load(); umsg.value = '已上传 ✓'
  } catch (e) { umsg.value = '失败：' + e.message }
}

async function transcribe() {
  if (!sel.value) { asrmsg.value = '先选视频'; return }
  asrmsg.value = '转写中…'
  try {
    const r = await api('POST', '/api/edit/transcribe', { path: sel.value })
    if (r.error) { asrmsg.value = '转写失败：' + r.error; return }
    lines.value = (r.segments || []).map((s) => s.start + '-' + s.end + ' ' + s.text).join('\n')
    asrmsg.value = '已转写 ' + (r.segments || []).length + ' 句'
  } catch (e) { asrmsg.value = '失败：' + e.message }
}

function parseLines(text) {
  const subsArr = [], voArr = []
  String(text || '').split('\n').forEach((raw) => {
    const line = raw.trim(); if (!line) return
    const m = line.match(/^(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s+(.+)$/) || line.match(/^(\d+(?:\.\d+)?)\s+(.+)$/)
    if (!m) return
    if (m.length === 4) { subsArr.push({ start: parseFloat(m[1]), end: parseFloat(m[2]), text: m[3] }); voArr.push({ start: parseFloat(m[1]), text: m[3] }) }
    else { voArr.push({ start: parseFloat(m[1]), text: m[2] }); subsArr.push({ start: parseFloat(m[1]), end: parseFloat(m[1]) + 3, text: m[2] }) }
  })
  return { subs: subsArr, vo: voArr }
}

async function apply() {
  if (!sel.value) { msg.value = '先选视频'; return }
  msg.value = '处理中…（配音/转码，较慢）'
  outUrl.value = ''
  const parsed = parseLines(lines.value)
  const spec = {
    path: sel.value,
    subtitles: subs.value ? parsed.subs : [],
    voiceover: vo.value ? parsed.vo : [],
    replace_audio: replace.value,
    bgm: bgm.value ? { path: bgm.value, volume: 0.18 } : {},
    aigc_label: label.value ? { text: 'AI生成', position: 'bottom_right', font_size: 30 } : null,
  }
  try {
    const r = await api('POST', '/api/edit/apply', spec)
    if (!r.output) { msg.value = r.note || '未生成'; return }
    msg.value = '完成 ✓'
    outUrl.value = mediaUrl('/api/edit/video?path=' + encodeURIComponent(r.output))
  } catch (e) { msg.value = '失败：' + e.message }
}
</script>

<template>
  <div>
    <div class="ed-cap">
      <div class="t">🪄 自动后期 · 配音 / 字幕 / BGM / 角标</div>
      <div class="d">给一段<b>已有的视频自动</b>加配音、烧字幕、配背景音乐、加合规角标。强项是<b>批量、自动化、与生成保持一致</b>（复用同一套配音音色 / 字幕样式 / AIGC 角标），本地一体、不用导出再导回。</div>
      <span class="ed-line ed-can">✅ 适合：转写台词（ASR） · 烧录字幕 · TTS 配音 / 旁白替换 · 背景音乐 · AIGC 角标 · 批量 / 无人值守</span>
      <span class="ed-line ed-cant">✂️ 要剪辑 / 裁剪 / 拼接 / 变速 / 转场 / 特效 / 调色 / 口型同步 → 请用剪映（CapCut）等专业剪辑器，这里不做。</span>
    </div>

    <div class="panel">
      <h2><span class="ed-step">1</span>导入视频</h2>
      <div class="muted">从 assets/imports/ 选一个，或直接上传（小文件较快）。</div>
      <label>选择已导入视频</label>
      <select v-model="sel">
        <option v-if="!videos.length" value="">（无，请上传）</option>
        <option v-for="v in videos" :key="v" :value="v">{{ v }}</option>
      </select>
      <label>或上传视频</label>
      <input ref="fileInput" type="file" accept="video/*" />
      <div style="margin-top:6px"><button class="ghost" @click="upload">上传</button> <span class="muted">{{ umsg }}</span></div>
    </div>

    <div class="panel">
      <h2><span class="ed-step">2</span>台词 / 字幕 <span class="muted" style="font-weight:400;font-size:12px">（可选）</span></h2>
      <div class="muted">点「转写」自动识别台词，或手填。这份台词用于烧字幕 / 替换配音。</div>
      <div class="row" style="margin-top:8px"><button class="ghost" @click="transcribe">🎙 转写台词（ASR）</button> <span class="muted">{{ asrmsg }}</span></div>
      <label>每行一句：<code>开始-结束 文本</code>（如 <code>0-3 我命由我不由天</code>）</label>
      <textarea v-model="lines" style="min-height:160px"></textarea>
    </div>

    <div class="panel">
      <h2><span class="ed-step">3</span>处理并输出</h2>
      <label class="chk"><input type="checkbox" v-model="subs" /> 烧录中文字幕</label>
      <label class="chk"><input type="checkbox" v-model="vo" /> 用 TTS 替换 / 添加配音（按上面台词）</label>
      <label class="chk"><input type="checkbox" v-model="replace" /> 替换原声 <span class="muted">（取消=叠加在原声之上）</span></label>
      <label>背景音乐 BGM 路径 <span class="muted" style="font-weight:400">（可选）</span></label>
      <input v-model="bgm" placeholder="assets/bgm/xxx.mp3" />
      <label class="chk" style="margin-top:10px"><input type="checkbox" v-model="label" /> 加 AIGC 合规角标</label>
      <div style="margin-top:12px"><button class="act" @click="apply">生成成片</button> <span class="muted">{{ msg }}</span></div>
      <div v-if="outUrl" style="margin-top:10px"><a class="ghost" :href="outUrl" target="_blank" style="text-decoration:none">查看/下载成片</a></div>
    </div>
  </div>
</template>
