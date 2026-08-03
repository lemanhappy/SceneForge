<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { AlertTriangle, Check, ChevronDown, ChevronUp, Clapperboard, History, Image, Languages, Plus, RefreshCw, Save, Sparkles, Trash2 } from '@lucide/vue'
import { api, mediaUrl } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { openLightbox } from '../lib/lightbox.js'
import {
  isChineseDominant, localizeAudioTags, nonChineseStoryboardFields,
  reviewableAudioPrompt, reviewableChineseText, reviewableVisualPrompt,
} from '../lib/language.js'

const props = defineProps({
  sid: String,
  dirty: { type: Boolean, default: false },
  saving: { type: Boolean, default: false },
  saveMessage: { type: String, default: '' },
  mediaByScene: { type: Object, default: () => ({}) },
  mediaRevision: { type: Number, default: 0 },
  busy: { type: Boolean, default: false },
  readOnly: { type: Boolean, default: false },
})
const emit = defineEmits(['save', 'generate-keyframe', 'refresh'])
// scenes: [{ scene_index, shots: [{ director_desc, duration_sec, beats, visual_desc, ... }] }]
const scenes = defineModel({ type: Array, default: () => [] })

const POS = [['', '位置(默认居中)'], ['top', '顶部'], ['center', '居中'], ['bottom', '底部']]
const blankBeat = (start = 0, end = 5) => ({ start_sec: start, end_sec: end, action: '', performance: '', camera: '' })
const blank = () => ({
  duration_sec: 5, director_desc: '', beats: [blankBeat()], visual_desc: '',
  visual_style_text: '', avoid_text: '', audio_desc: '', screen_text: '', screen_text_pos: '',
})

const splitList = (value) => String(value || '').split(/[,，;；\n]/).map((x) => x.trim()).filter(Boolean)
function shotPayload(shot) {
  return {
    duration_sec: Number(shot.duration_sec) || 5,
    director_desc: shot.director_desc || '',
    beats: (shot.beats || []).map((beat) => ({
      start_sec: Number(beat.start_sec) || 0, end_sec: Number(beat.end_sec) || 0,
      action: beat.action || '', performance: beat.performance || '', camera: beat.camera || '',
    })),
    visual_desc: shot.visual_desc || '', visual_style: splitList(shot.visual_style_text),
    avoid: splitList(shot.avoid_text), audio_desc: shot.audio_desc || '',
    screen_text: shot.screen_text || '', screen_text_pos: shot.screen_text_pos || '',
  }
}

const selectedSceneIndex = ref(0)
const selectedShotIndex = ref(0)
const editedVisualPrompts = new WeakSet()
const editedChineseFields = new WeakMap()
const structureKey = computed(() => scenes.value.map((scene) => `${scene.scene_index}:${scene.shots.length}`).join('|'))
const selectedEntry = computed(() => {
  const scene = scenes.value[selectedSceneIndex.value]
  if (!scene) return null
  const idx = Math.min(selectedShotIndex.value, Math.max(0, scene.shots.length - 1))
  const shot = scene.shots[idx]
  return shot ? { scene, shot, idx } : null
})
const totalShots = computed(() => scenes.value.reduce((sum, scene) => sum + scene.shots.length, 0))
watch(structureKey, () => {
  if (!scenes.value.length) return
  selectedSceneIndex.value = Math.min(selectedSceneIndex.value, scenes.value.length - 1)
  const scene = scenes.value[selectedSceneIndex.value]
  selectedShotIndex.value = Math.min(selectedShotIndex.value, Math.max(0, scene.shots.length - 1))
}, { immediate: true })

function selectShot(sceneIndex, shotIndex) { selectedSceneIndex.value = sceneIndex; selectedShotIndex.value = shotIndex }
function visibleVisualPrompt(shot) {
  if (editedVisualPrompts.has(shot)) return String(shot.visual_desc || '')
  return reviewableVisualPrompt(shot.visual_desc, shot.director_desc)
}
function updateVisualPrompt(shot, value) {
  editedVisualPrompts.add(shot)
  shot.visual_desc = value
}
function chineseFieldEdited(owner, field) { return editedChineseFields.get(owner)?.has(field) || false }
function visibleChineseField(owner, field, fallback) {
  const value = String((owner && owner[field]) || '')
  if (chineseFieldEdited(owner, field) || !value.trim() || isChineseDominant(value)) return value
  return fallback || reviewableChineseText(value)
}
function updateChineseField(owner, field, value) {
  let fields = editedChineseFields.get(owner)
  if (!fields) { fields = new Set(); editedChineseFields.set(owner, fields) }
  fields.add(field)
  owner[field] = value
}
function visibleAudioPrompt(shot) {
  return chineseFieldEdited(shot, 'audio_desc') ? String(shot.audio_desc || '') : reviewableAudioPrompt(shot.audio_desc)
}
function shotMedia(scene, idx) {
  const manifestScene = props.mediaByScene[scene.scene_index] || { shots: [] }
  const entry = (manifestScene.shots || []).find((item) => Number(item.idx) === Number(idx)) || {}
  return entry.media || {}
}
function framePath(scene, idx) { return shotMedia(scene, idx)['first_frame.png'] || '' }
function frameUrl(scene, idx) {
  const path = framePath(scene, idx)
  if (!path) return ''
  return mediaUrl('/api/production/' + props.sid + '/file?path=' + encodeURIComponent(path) + '&v=' + (props.mediaRevision || 0))
}
function priorFramesReady(scene, idx) {
  return (scene.shots || []).slice(0, idx).every((_shot, priorIdx) => !!framePath(scene, priorIdx))
}
function keyframeDisabledReason(scene, idx) {
  if (props.readOnly) return '当前阶段仅可查看首帧'
  if (props.busy) return '当前已有生成任务，请稍候'
  if (props.dirty) return '请先保存分镜修改，再生成首帧'
  if (!priorFramesReady(scene, idx)) return '请先依次生成前一镜的首帧'
  return ''
}
function requestKeyframe(scene, idx) {
  if (keyframeDisabledReason(scene, idx)) return
  emit('generate-keyframe', {
    sceneIndex: Number(scene.scene_index),
    shotIndex: Number(idx),
    force: !!framePath(scene, idx),
  })
}
function addShot(scene, sceneIndex) { scene.shots.push(blank()); selectShot(sceneIndex, scene.shots.length - 1) }
function insertAfter(scene, idx, sceneIndex) { scene.shots.splice(idx + 1, 0, blank()); selectShot(sceneIndex, idx + 1) }
function addBeat(shot) {
  const beats = shot.beats || []
  const last = beats[beats.length - 1]
  const start = last ? Number(last.end_sec) || 0 : 0
  const duration = Number(shot.duration_sec) || 5
  const end = Math.min(15, Math.max(start + 1, duration))
  if (end <= start) return
  if (!shot.beats) shot.beats = beats
  shot.duration_sec = Math.max(duration, end)
  shot.beats.push(blankBeat(start, end))
}
function delBeat(shot, idx) { shot.beats.splice(idx, 1) }
async function delShot(scene, idx, sceneIndex) {
  if (scene.shots.length <= 1) { await confirmModal('每个场景至少保留一个镜头。', { okText: '知道了' }); return }
  if (await confirmModal('确定删除这个镜头？删除后点「保存」才会生效。', { okText: '删除', danger: true })) {
    scene.shots.splice(idx, 1)
    selectShot(sceneIndex, Math.min(idx, scene.shots.length - 1))
  }
}

// 单镜 AI 重写：把该场景当前镜头作为上下文，让 AI 只重写这一镜，结果填回输入框（保存才生效）
const rw = reactive({}) // key -> { open, hint, busy, msg }
const keyOf = (scene, idx) => scene.scene_index + '_' + idx
function st(scene, idx) { const k = keyOf(scene, idx); if (!rw[k]) rw[k] = { open: false, hint: '', busy: false, msg: '' }; return rw[k] }
function toggleRewrite(scene, idx) {
  const s = st(scene, idx)
  s.open = !s.open
  s.msg = ''
  if (s.open && nonChineseStoryboardFields(scene.shots[idx]).length && !s.hint.trim()) {
    s.hint = '仅将当前镜头的导演稿、执行节拍、画面提示词、画面风格、避免项和声音描述转换为简体中文。保持原有剧情、时间、镜头调度、角色与资产标识、台词含义及连续性约束不变，不增加新动作。'
  }
}
async function doRewrite(scene, idx) {
  const s = st(scene, idx)
  s.busy = true; s.msg = '✨ AI 重写中…'
  try {
    const r = await api('POST', '/api/production/' + props.sid + '/rewrite-shot', {
      scene_index: scene.scene_index, shot_index: idx, instruction: s.hint || '',
      shots: scene.shots.map(shotPayload),
    })
    if (r && r.ok && r.shot) {
      Object.assign(scene.shots[idx], {
        duration_sec: Number(r.shot.duration_sec) || 5,
        director_desc: r.shot.director_desc || '', beats: r.shot.beats || [],
        visual_desc: r.shot.visual_desc || '', audio_desc: localizeAudioTags(r.shot.audio_desc),
        visual_style_text: (r.shot.visual_style || []).join(', '),
        avoid_text: (r.shot.avoid || []).join(', '),
        screen_text: r.shot.screen_text || '', screen_text_pos: r.shot.screen_text_pos || '',
      })
      s.open = false; s.hint = ''; s.msg = ''
    } else { s.msg = '失败：' + ((r && (r.note || r.error)) || '未知错误') }
  } catch (e) { s.msg = '失败：' + ((e.body && (e.body.note || e.body.error)) || e.message) }
  s.busy = false
}

const keyframeHistory = reactive({})
const historyKey = (scene, idx) => scene.scene_index + '_' + idx
function keyframeHistoryState(scene, idx) {
  const key = historyKey(scene, idx)
  if (!keyframeHistory[key]) keyframeHistory[key] = { open: true, loading: false, loaded: false, versions: [], error: '' }
  return keyframeHistory[key]
}
async function loadKeyframeHistory(scene, idx, force = false) {
  const state = keyframeHistoryState(scene, idx)
  if (state.loading || (state.loaded && !force)) return
  state.loading = true; state.error = ''
  try {
    const query = '?scene_index=' + scene.scene_index + '&shot_index=' + idx + '&artifact_type=keyframe'
    const result = await api('GET', '/api/production/' + props.sid + '/artifact-versions' + query)
    const versions = result.versions || []
    state.versions = versions.map((item, index) => ({ ...item, display_version: versions.length - index }))
    state.loaded = true
  } catch (e) {
    state.error = e.status === 409 ? '当前项目尚未启用版本记录' : ('加载失败：' + e.message)
    state.loaded = true
  }
  state.loading = false
}
async function toggleKeyframeHistory(scene, idx) {
  const state = keyframeHistoryState(scene, idx)
  state.open = !state.open
  if (state.open) await loadKeyframeHistory(scene, idx)
}
const keyframeVersionUrl = (item) => mediaUrl('/api/production/' + props.sid + '/artifact-versions/' + item.artifact_id + '/file')
const keyframeStatusLabel = (status) => ({ active: '当前', stale: '已过期', archived: '历史' }[status] || status)
function formatVersionTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}
async function selectKeyframeVersion(scene, idx, item) {
  if (!await confirmModal('使用首帧 v' + (item.display_version || item.version) + '？当前版本仍会保留。', { okText: '使用此版本' })) return
  const state = keyframeHistoryState(scene, idx)
  try {
    await api('POST', '/api/production/' + props.sid + '/artifact-versions/' + item.artifact_id + '/rollback')
    await loadKeyframeHistory(scene, idx, true)
    emit('refresh')
  } catch (e) { state.error = '切换失败：' + ((e.body && e.body.error) || e.message) }
}
watch(() => {
  const entry = selectedEntry.value
  return entry ? `${props.sid}:${historyKey(entry.scene, entry.idx)}:${framePath(entry.scene, entry.idx)}` : ''
}, () => {
  const entry = selectedEntry.value
  if (entry) loadKeyframeHistory(entry.scene, entry.idx)
}, { immediate: true })
</script>

<template>
  <div id="sb_edit" class="sb-board-editor" :class="{ 'is-readonly': readOnly }" :data-storyboard-mode="readOnly ? 'readonly' : 'editable'">
    <header v-if="!readOnly" class="sb-board-toolbar">
      <div><strong>{{ totalShots }} 个镜头</strong><span>按场景检查并逐镜编辑</span></div>
      <div class="sb-inspector-actions">
        <button class="act sb-save-action" type="button" :disabled="saving || !dirty"
          :title="dirty ? '保存全部分镜修改' : '当前没有未保存的修改'" @click="emit('save')"><Save :size="14" />{{ saving ? '保存中…' : '保存分镜' }}</button>
        <span v-if="dirty && !saving" class="sb-save-status is-dirty" role="status"><AlertTriangle :size="13" />有未保存的修改，保存后才会用于生成</span>
        <span v-else-if="saveMessage && !saving" class="sb-save-status" role="status">{{ saveMessage }}</span>
      </div>
    </header>

    <section v-for="(scene, sceneIndex) in scenes" :key="scene.scene_index" class="sb-board-scene">
      <div class="sb-board-scene-head">
        <div><strong>场景 {{ Number(scene.scene_index) + 1 }}</strong><span>{{ scene.shots.length }} 个镜头</span></div>
        <button v-if="!readOnly" class="ghost" type="button" @click="addShot(scene, sceneIndex)"><Plus :size="14" />添加镜头</button>
      </div>

      <div class="sb-board-grid">
        <article v-for="(shot, idx) in scene.shots" :key="idx" class="sb-board-card"
          :class="{ selected: selectedSceneIndex === sceneIndex && selectedShotIndex === idx }">
          <button class="sb-board-preview" type="button" @click="selectShot(sceneIndex, idx)">
            <img v-if="framePath(scene, idx)" :src="frameUrl(scene, idx)" alt="" />
            <span v-else class="sb-board-placeholder"><Clapperboard :size="25" /><small>首帧待生成</small></span>
            <span class="statetag" :class="framePath(scene, idx) ? 'st-done' : 'st-pend'">{{ framePath(scene, idx) ? '已有首帧' : '待生成' }}</span>
          </button>
          <button class="gen-card-select" type="button" @click="selectShot(sceneIndex, idx)">
            <span><strong>镜 {{ idx + 1 }}</strong><small>{{ Number(shot.duration_sec || 5).toFixed(1) }}s</small></span>
            <span class="sb-card-desc">{{ reviewableVisualPrompt(shot.visual_desc, shot.director_desc) || '旧版分镜待转换为中文' }}</span>
          </button>
        </article>
      </div>

      <section v-if="selectedEntry && selectedSceneIndex === sceneIndex" class="sb-board-detail">
        <div class="generation-detail-head">
          <div><span class="detail-kicker">{{ readOnly ? '当前镜头详情' : '当前镜头编辑' }}</span><strong>镜 {{ selectedEntry.idx + 1 }}</strong><small>场景 {{ Number(selectedEntry.scene.scene_index) + 1 }} · {{ Number(selectedEntry.shot.duration_sec || 5).toFixed(1) }}s</small></div>
          <div v-if="!readOnly" class="sb-tools">
            <button class="ghost" :title="nonChineseStoryboardFields(selectedEntry.shot).length ? '将当前镜头的旧版英文审核字段转换为简体中文' : '让 AI 只重写这一镜'"
              @click="toggleRewrite(selectedEntry.scene, selectedEntry.idx)">
              <Languages v-if="nonChineseStoryboardFields(selectedEntry.shot).length" :size="14" />
              <Sparkles v-else :size="14" />{{ nonChineseStoryboardFields(selectedEntry.shot).length ? '转为中文' : 'AI 重写' }}
            </button>
            <button class="ghost" title="在此镜后插入新镜头" @click="insertAfter(selectedEntry.scene, selectedEntry.idx, selectedSceneIndex)"><Plus :size="14" />插入</button>
            <button class="iconbtn danger-text" title="删除此镜头" aria-label="删除此镜头" @click="delShot(selectedEntry.scene, selectedEntry.idx, selectedSceneIndex)"><Trash2 :size="15" /></button>
          </div>
        </div>

        <div v-if="!readOnly && st(selectedEntry.scene, selectedEntry.idx).open" class="sb-rwbox">
          <span v-if="nonChineseStoryboardFields(selectedEntry.shot).length" class="sb-language-issues"
            :title="nonChineseStoryboardFields(selectedEntry.shot).join('、')">
            <AlertTriangle :size="13" />检测到 {{ nonChineseStoryboardFields(selectedEntry.shot).length }} 处旧版英文提示词，重写后将统一改为简体中文
          </span>
          <input v-model="st(selectedEntry.scene, selectedEntry.idx).hint" :disabled="st(selectedEntry.scene, selectedEntry.idx).busy"
            placeholder="输入改写方向；留空由 AI 优化" @keydown.enter="doRewrite(selectedEntry.scene, selectedEntry.idx)" />
          <button class="act" :disabled="st(selectedEntry.scene, selectedEntry.idx).busy" @click="doRewrite(selectedEntry.scene, selectedEntry.idx)">{{ st(selectedEntry.scene, selectedEntry.idx).busy ? '重写中…' : '重写' }}</button>
          <button class="ghost" :disabled="st(selectedEntry.scene, selectedEntry.idx).busy" @click="toggleRewrite(selectedEntry.scene, selectedEntry.idx)">取消</button>
          <span class="muted">{{ st(selectedEntry.scene, selectedEntry.idx).msg }}</span>
        </div>

        <div class="sb-board-detail-grid">
          <div class="sb-board-fields" aria-label="分镜详情">
            <div class="sb-detail-column-head">
              <div><strong>分镜详情</strong><span>{{ readOnly ? '查看当前镜头的导演稿、节拍与模型提示词' : '直接修改当前镜头，保存后用于后续生成' }}</span></div>
            </div>
            <div class="sb-duration-field"><label class="sbl">计划时长</label><input class="sb-duration" type="number" min="1" max="15" step="0.5" v-model.number="selectedEntry.shot.duration_sec" :readonly="readOnly" /><span>秒</span></div>
            <label class="sbl">导演稿（中文审核稿）</label>
            <textarea class="sb-director" rows="6" v-model="selectedEntry.shot.director_desc" :readonly="readOnly" placeholder="描述运镜、动作、眼神、呼吸、微表情和停顿"></textarea>
            <label class="sbl">台词 / 音频（中文审核稿）</label>
            <textarea class="sb-ad" rows="4" :value="visibleAudioPrompt(selectedEntry.shot)" :readonly="readOnly"
              @input="updateChineseField(selectedEntry.shot, 'audio_desc', $event.target.value)"></textarea>

            <details class="sb-exec">
              <summary>执行节拍与模型提示词</summary>
              <div class="sb-beats">
                <div v-for="(beat, bi) in (selectedEntry.shot.beats || [])" :key="bi" class="sb-beat">
                  <div class="sb-beat-head">
                    <strong>节拍 {{ bi + 1 }}</strong>
                    <span class="sb-time"><input type="number" min="0" step="0.5" v-model.number="beat.start_sec" :readonly="readOnly" /> 至 <input type="number" min="0" step="0.5" v-model.number="beat.end_sec" :readonly="readOnly" /> 秒</span>
                    <button v-if="!readOnly" type="button" class="sb-beat-del" @click="delBeat(selectedEntry.shot, bi)">删除</button>
                  </div>
                  <label class="sbl">镜头运动</label><input :value="visibleChineseField(beat, 'camera', '旧版镜头运动待转换为中文')" :readonly="readOnly"
                    placeholder="从近景缓慢推进到更紧的特写" @input="updateChineseField(beat, 'camera', $event.target.value)" />
                  <label class="sbl">可见动作</label><textarea rows="2" :value="visibleChineseField(beat, 'action', '旧版可见动作待转换为中文')" :readonly="readOnly"
                    placeholder="描述人物或物体在画面中发生的动作" @input="updateChineseField(beat, 'action', $event.target.value)"></textarea>
                  <label class="sbl">细腻表演</label><textarea rows="2" :value="visibleChineseField(beat, 'performance', '旧版细腻表演待转换为中文')" :readonly="readOnly"
                    placeholder="描述眼神、呼吸、眉心、嘴唇、吞咽、眼泪和停顿" @input="updateChineseField(beat, 'performance', $event.target.value)"></textarea>
                </div>
                <button v-if="!readOnly" type="button" class="ghost sb-add-beat" @click="addBeat(selectedEntry.shot)"><Plus :size="14" />添加表演节拍</button>
              </div>
              <label class="sbl">画面提示词（中文审核稿）</label><textarea class="sb-vd" rows="4"
                :value="visibleVisualPrompt(selectedEntry.shot)" :readonly="readOnly"
                placeholder="用中文描述景别、构图、光线、人物位置和动作变化"
                @input="updateVisualPrompt(selectedEntry.shot, $event.target.value)"></textarea>
              <div class="sb-grid2">
                <div><label class="sbl">画面风格</label><input :value="visibleChineseField(selectedEntry.shot, 'visual_style_text', '旧版画面风格待转换为中文')" :readonly="readOnly"
                  placeholder="电影感、浅景深、柔和自然光" @input="updateChineseField(selectedEntry.shot, 'visual_style_text', $event.target.value)" /></div>
                <div><label class="sbl">避免项</label><input :value="visibleChineseField(selectedEntry.shot, 'avoid_text', '旧版避免项待转换为中文')" :readonly="readOnly"
                  placeholder="不要重影、不要夸张表演、不要快速剪辑" @input="updateChineseField(selectedEntry.shot, 'avoid_text', $event.target.value)" /></div>
              </div>
              <label class="sbl">画面文字与位置</label>
              <div class="row">
                <input class="sb-st" v-model="selectedEntry.shot.screen_text" :readonly="readOnly" placeholder="留空则不叠加文字" />
                <select class="sb-stp" v-model="selectedEntry.shot.screen_text_pos" :disabled="readOnly" title="画面文字位置"><option v-for="[v, l] in POS" :key="v" :value="v">{{ l }}</option></select>
              </div>
            </details>
          </div>

          <aside class="sb-board-keyframe" aria-label="首帧历史版本">
            <div class="sb-detail-column-head sb-keyframe-head">
              <div><strong>首帧历史版本</strong><span>{{ framePath(selectedEntry.scene, selectedEntry.idx) ? (readOnly ? '当前版本置顶，可查看或切换历史版本' : '当前版本置顶，可重新生成或切换历史版本') : (readOnly ? '当前镜头尚未生成首帧' : '先生成当前镜头首帧，后续版本会保留在这里') }}</span></div>
              <button v-if="!readOnly" class="ghost" type="button"
                :disabled="!!keyframeDisabledReason(selectedEntry.scene, selectedEntry.idx)"
                :title="keyframeDisabledReason(selectedEntry.scene, selectedEntry.idx) || (framePath(selectedEntry.scene, selectedEntry.idx) ? '生成新版本，当前首帧会进入历史记录' : '只生成当前镜头首帧')"
                @click="requestKeyframe(selectedEntry.scene, selectedEntry.idx)">
                <RefreshCw v-if="framePath(selectedEntry.scene, selectedEntry.idx)" :size="14" />
                <Image v-else :size="14" />{{ framePath(selectedEntry.scene, selectedEntry.idx) ? '重新生成' : '生成首帧' }}
              </button>
            </div>
            <div v-if="framePath(selectedEntry.scene, selectedEntry.idx)" class="sb-keyframe-current-wrap">
              <span class="sb-current-version"><Check :size="12" />当前使用</span>
              <button type="button" class="sb-keyframe-current zoom" title="查看当前首帧大图"
                @click="openLightbox(frameUrl(selectedEntry.scene, selectedEntry.idx))"><img :src="frameUrl(selectedEntry.scene, selectedEntry.idx)" alt="当前镜头首帧" /></button>
            </div>
            <div v-else class="sb-keyframe-empty"><Image :size="24" /><span>{{ readOnly ? '尚未生成首帧' : (keyframeDisabledReason(selectedEntry.scene, selectedEntry.idx) || '尚未生成首帧') }}</span></div>

            <div class="sb-keyframe-history">
              <button class="shot-history-toggle" type="button" :aria-expanded="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).open"
                @click="toggleKeyframeHistory(selectedEntry.scene, selectedEntry.idx)">
                <span class="shot-history-title"><History :size="15" /><span><strong>版本记录</strong><small>查看并选择当前镜头使用的首帧</small></span></span>
                <ChevronUp v-if="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).open" :size="15" />
                <ChevronDown v-else :size="15" />
              </button>
              <div v-if="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).open" class="shot-history">
                <div v-if="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).loading" class="muted">加载中…</div>
                <div v-else-if="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).error" class="muted">{{ keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).error }}</div>
                <div v-else-if="keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).versions.length" class="version-group">
                  <div class="version-kind"><span>首帧版本</span><small>{{ keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).versions.length }} 个版本</small></div>
                  <article v-for="item in keyframeHistoryState(selectedEntry.scene, selectedEntry.idx).versions" :key="item.artifact_id" class="version-card" :class="{ active: item.status === 'active' }">
                    <button type="button" class="version-preview version-image-preview" title="查看大图" @click="openLightbox(keyframeVersionUrl(item))"><img :src="keyframeVersionUrl(item)" alt="" /></button>
                    <div class="version-card-body">
                      <div class="version-card-head"><strong>v{{ item.display_version || item.version }}</strong><span class="version-status" :class="'vs-' + item.status">{{ keyframeStatusLabel(item.status) }}</span></div>
                      <span class="version-time">{{ formatVersionTime(item.created_at) }}</span>
                      <span v-if="item.status === 'active'" class="version-current"><Check :size="12" />当前使用</span>
                      <button v-else class="version-select" type="button" :disabled="busy" @click="selectKeyframeVersion(selectedEntry.scene, selectedEntry.idx, item)">选择此版本</button>
                    </div>
                  </article>
                </div>
                <div v-else class="muted">暂无首帧历史版本</div>
              </div>
            </div>
          </aside>
        </div>
      </section>
    </section>
  </div>
</template>
