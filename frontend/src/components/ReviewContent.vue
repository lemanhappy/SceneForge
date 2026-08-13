<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import {
  AlertTriangle, ArrowDown, ArrowUp, Boxes, Calculator, Captions, Check, CheckCircle2, CheckSquare, ChevronDown, ChevronUp, Clapperboard,
  Columns2, Download, Film, History, Lock, MapPin, MessageSquarePlus, Pencil, RefreshCw, Save, Share2, SlidersHorizontal, Trash2,
  RotateCcw, Scissors, UserRound, Volume2, X,
} from '@lucide/vue'
import { api, mediaUrl, watchJob } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { openLightbox } from '../lib/lightbox.js'
import {
  localizeAudioTags, normalizeAudioTags, reviewableAudioPrompt,
  reviewableVisualPrompt,
} from '../lib/language.js'
import { stageInfo, qualityBadge } from '../lib/stages.js'
import StoryboardEditor from './StoryboardEditor.vue'

const props = defineProps({
  sid: String,
  snap: Object,
  view: { type: String, default: '' },
  canRevise: { type: Boolean, default: false },
  canPublish: { type: Boolean, default: false },
  canClean: { type: Boolean, default: false },
  canCost: { type: Boolean, default: false },
  costLabel: { type: String, default: '' },
})
const emit = defineEmits(['refresh', 'stats', 'publish', 'clean', 'cost', 'reopen'])
const mediaRevision = ref(Date.now())
const finalUrl = computed(() => mediaUrl('/api/production/' + props.sid + '/video?v=' + mediaRevision.value))
const hasFinal = computed(() => !!(props.snap && props.snap.has_final))
const shareEnabled = computed(() => !!(props.snap && props.snap.publish_capabilities && props.snap.publish_capabilities.share_enabled))

// 成片仍使用阶段级 AI 修改；剧本、分镜和镜头提示词均在正文原位编辑。
const REVISE_LABEL = { final: '成片' }
const canReviseHere = computed(() => props.canRevise && props.view === gate.value && !!REVISE_LABEL[props.view])
const reviseLabel = computed(() => REVISE_LABEL[props.view] || '')
const reviseOpen = ref(false)
const reviseText = ref('')
const reviseMsg = ref('')
async function submitRevise() {
  const t = reviseText.value.trim(); if (!t) return
  reviseOpen.value = false; reviseText.value = ''; reviseMsg.value = ''
  try {
    const r = await api('POST', '/api/production/' + props.sid + '/revise', { instruction: t })
    if (r && r.accepted === false) { reviseMsg.value = '上一步还在处理中，请稍候。'; return }
    emit('refresh')
  } catch (e) { reviseMsg.value = '失败：' + ((e.body && e.body.error) || e.message) }
}

const script = ref({})
const sb = ref({})
const man = ref({})
const metrics = ref({ summary: {}, shots: [], models: [] })
const loading = ref(true)
const err = ref('')
const editScenes = ref([])
const sbMsg = ref('')
const storyboardBaseline = ref('[]')
const storyboardFingerprint = (scenes) => JSON.stringify(scenes || [])
const storyboardDirty = computed(() => storyboardFingerprint(editScenes.value) !== storyboardBaseline.value)

const scriptEditing = ref(false)
const scriptDraft = ref('')
const scriptBaseline = ref('')
const scriptSaving = ref(false)
const scriptMsg = ref('')
const scriptEditorEl = ref(null)
const scriptDirty = computed(() => scriptDraft.value !== scriptBaseline.value)

const gate = computed(() => stageInfo(props.snap || {}).gate)
const hasStoryboard = computed(() => !!(sb.value.scenes && sb.value.scenes.length))
const scriptOpen = computed(() => !hasStoryboard.value || gate.value === 'script')
const sbEditable = computed(() => gate.value === 'storyboard' && !(props.snap && props.snap.busy))
const scriptEditable = computed(() => props.view === 'script' && gate.value === 'script' && !(props.snap && props.snap.busy))
const canRequestScriptEdit = computed(() => props.view === 'script' && gate.value !== 'script' && hasStoryboard.value && !(props.snap && props.snap.busy))
const canRequestStoryboardEdit = computed(() => props.view === 'storyboard' && gate.value !== 'storyboard' && hasStoryboard.value && !(props.snap && props.snap.busy))

const mediaByScene = computed(() => { const m = {}; (man.value.scenes || []).forEach((sc) => { m[sc.scene_index] = sc }); return m })
const anyVideo = computed(() => (man.value.scenes || []).some((sc) => (sc.shots || []).some((x) => (x.media || {})['video.mp4'])))
// 看板：只要有关键帧或视频就展示（生成过程中逐步从「待生成」长成「帧就绪」「视频」）
const hasFrames = computed(() => (man.value.scenes || []).some((sc) => (sc.shots || []).some((x) => { const m = x.media || {}; return m['video.mp4'] || m['first_frame.png'] })))
const showBoard = computed(() => hasFrames.value || ['shot_video', 'final', 'completed'].includes(gate.value))
const storyboardEntries = computed(() => (sb.value.scenes || []).flatMap((scene) =>
  (scene.shots || []).map((shot) => ({ key: `${scene.scene_index}_${shot.idx}`, scene, shot }))))
const selectedGenerationKey = ref('')
watch(storyboardEntries, (entries) => {
  const keys = new Set(entries.map((entry) => entry.key))
  if (!keys.has(selectedGenerationKey.value)) selectedGenerationKey.value = entries[0] ? entries[0].key : ''
}, { immediate: true })
const selectedGeneration = computed(() => storyboardEntries.value.find((entry) => entry.key === selectedGenerationKey.value) || null)
const totalDuration = computed(() => storyboardEntries.value.reduce((sum, entry) => sum + Number(entry.shot.duration_sec || 5), 0))
const qualityIssueCount = computed(() => storyboardEntries.value.filter((entry) => {
  const badge = qualityBadge(shotEntry(entry.scene, entry.shot.idx).quality)
  return badge && badge.cls !== 'q-ok'
}).length)
const allQualityPass = computed(() => totals.value.videos === totals.value.total && qualityIssueCount.value === 0)
const metricsSummary = computed(() => metrics.value.summary || {})
const metricCost = computed(() => metricsSummary.value.cost || {})
const reworkSavings = computed(() => metricsSummary.value.local_rework_savings || {})
const editPlan = ref(null)
const editPlanBaseline = ref('')
const editPlanOpen = ref(false)
const editPlanLoading = ref(false)
const editPlanBusy = ref(false)
const editPlanMsg = ref('')
const editPlanError = ref('')
const subtitleTimeline = ref(null)
const subtitleBaseline = ref('')
const subtitleOpen = ref(false)
const subtitleLoading = ref(false)
const subtitleBusy = ref(false)
const subtitleMsg = ref('')
const subtitleError = ref('')
const subtitleDownloadUrl = computed(() => mediaUrl('/api/production/' + props.sid + '/subtitles/file'))
const regenReasons = reactive({})
const batchMode = ref(false)
const batchSelected = reactive({})
const batchReason = ref('visual_mismatch')
const batchPreview = ref(null)
const batchBusy = ref(false)
const batchMsg = ref('')
const batchSettingsOpen = ref(false)
const reworkLocks = reactive({ identity: true, composition: false, motion: false, audio: true })
const regenReasonOptions = [
  { value: 'visual_mismatch', label: '画面不符合预期', dimensions: ['visual'] },
  { value: 'character_identity', label: '人物形象不一致', dimensions: ['identity'] },
  { value: 'motion_error', label: '动作或运镜错误', dimensions: ['motion'] },
  { value: 'continuity', label: '镜头连续性问题', dimensions: ['continuity'] },
  { value: 'composition', label: '构图需要调整', dimensions: ['composition'] },
  { value: 'technical_quality', label: '清晰度或技术质量', dimensions: ['technical'] },
  { value: 'other', label: '其他原因', dimensions: [] },
]
function regenReasonKey(sceneIdx, shotIdx) { return `${sceneIdx}_${shotIdx}` }
function regenReason(sceneIdx, shotIdx) {
  const key = regenReasonKey(sceneIdx, shotIdx)
  if (!regenReasons[key]) regenReasons[key] = 'visual_mismatch'
  return regenReasons[key]
}
function regenDimensions(reason) {
  const item = regenReasonOptions.find((option) => option.value === reason)
  return item ? item.dimensions : []
}
const lockedDimensions = computed(() => Object.keys(reworkLocks).filter((key) => reworkLocks[key]))
const batchSelection = computed(() => storyboardEntries.value.filter((entry) => !!batchSelected[entry.key]))
const batchSelectedCount = computed(() => batchSelection.value.length)
function setBatchMode(enabled) {
  batchMode.value = enabled
  batchMsg.value = ''
  batchPreview.value = null
  batchSettingsOpen.value = false
  if (!enabled) Object.keys(batchSelected).forEach((key) => delete batchSelected[key])
}
function toggleBatchShot(sceneIdx, shotIdx, checked) {
  const key = `${sceneIdx}_${shotIdx}`
  if (checked) batchSelected[key] = true
  else delete batchSelected[key]
  batchPreview.value = null
  batchMsg.value = ''
}
function regenerationEstimateText(estimate) {
  if (!estimate || !estimate.available) return '费用待模型单价配置'
  const lower = Number(estimate.estimated_lower_bound)
  const upper = Number(estimate.estimated_upper_bound)
  if (!Number.isFinite(lower) || !Number.isFinite(upper)) return '费用待模型单价配置'
  if (Math.abs(lower - upper) > 0.001) return `${money(lower, estimate.currency)}–${money(upper, estimate.currency)}`
  return money(upper, estimate.currency)
}
function affectedShotText(shots) {
  const groups = new Map()
  ;(shots || []).forEach((item) => {
    const scene = Number(item.scene_index) + 1
    if (!groups.has(scene)) groups.set(scene, [])
    groups.get(scene).push(Number(item.shot_idx) + 1)
  })
  return [...groups.entries()].map(([scene, indexes]) => `场景 ${scene}：镜 ${indexes.join('、')}`).join('；')
}
function cumulativeSavingsText(savings) {
  if (!savings || !Number(savings.completed_batches)) return ''
  const parts = [`局部返工 ${savings.completed_batches} 批`, `累计少生成 ${savings.avoided_shot_count || 0} 镜`]
  if (savings.estimated_generation_seconds_saved != null) parts.push(`预计少等 ${formatSeconds(savings.estimated_generation_seconds_saved)}`)
  if (savings.estimated_cost_saved_upper_bound != null) parts.push(`预计少花 ${money(savings.estimated_cost_saved_upper_bound, savings.currency)}`)
  return parts.join(' · ')
}
function editPlanFingerprint(plan) {
  if (!plan) return ''
  return JSON.stringify({
    transition: plan.transition || {},
    clips: (plan.clips || []).map((item) => ({
      clip_id: item.clip_id,
      trim_start: Number(item.trim_start),
      trim_end: Number(item.trim_end),
    })),
  })
}
function subtitleFingerprint(timeline) {
  if (!timeline) return ''
  return JSON.stringify((timeline.lines || []).map((line) => ({
    line_id: line.line_id,
    text: line.text,
    start: Number(line.start),
    end: Number(line.end),
  })))
}
const subtitleDirty = computed(() => subtitleFingerprint(subtitleTimeline.value) !== subtitleBaseline.value)
function subtitlePosition(line) {
  const duration = Math.max(0.1, Number((subtitleTimeline.value || {}).duration) || totalDuration.value || 0.1)
  const left = Math.max(0, Math.min(100, Number(line.start) / duration * 100))
  const width = Math.max(1.5, Math.min(100 - left, (Number(line.end) - Number(line.start)) / duration * 100))
  return { left: `${left}%`, width: `${width}%` }
}
const editPlanDirty = computed(() => editPlanFingerprint(editPlan.value) !== editPlanBaseline.value)
const editPlanOutputDuration = computed(() => {
  const plan = editPlan.value
  if (!plan) return totalDuration.value
  const clips = plan.clips || []
  let duration = clips.reduce((sum, item) => sum + Math.max(0, Number(item.trim_end) - Number(item.trim_start)), 0)
  if ((plan.transition || {}).type === 'crossfade') {
    duration -= Math.max(0, clips.length - 1) * Number((plan.transition || {}).duration || 0)
  }
  return Math.max(0, duration)
})
const editPlanTransitionMax = computed(() => {
  const plan = editPlan.value
  if (!plan || (plan.transition || {}).type !== 'crossfade') return 2
  const durations = (plan.clips || []).map((item) => Math.max(0, Number(item.trim_end) - Number(item.trim_start)))
  if (durations.length < 2) return 2
  return Math.max(0.1, Math.min(2, Math.min(...durations) / 2))
})
const finalTimelineClips = computed(() => {
  if (editPlan.value && (editPlan.value.clips || []).length) {
    return editPlan.value.clips.map((item) => ({
      key: item.clip_id,
      label: `镜 ${Number(item.shot_idx) + 1}`,
      duration: Math.max(0, Number(item.trim_end) - Number(item.trim_start)),
    }))
  }
  return storyboardEntries.value.map((entry) => ({
    key: entry.key,
    label: `镜 ${Number(entry.shot.idx) + 1}`,
    duration: Number(entry.shot.duration_sec || 5),
  }))
})
async function loadEditPlan(force = false) {
  if (!hasFinal.value || editPlanLoading.value || (editPlan.value && !force)) return
  editPlanLoading.value = true
  editPlanError.value = ''
  try {
    const result = await api('GET', '/api/production/' + props.sid + '/edit-plan')
    editPlan.value = result.plan
    editPlanBaseline.value = editPlanFingerprint(result.plan)
  } catch (e) { editPlanError.value = (e.body && e.body.error) || e.message }
  finally { editPlanLoading.value = false }
}
async function toggleEditPlan() {
  editPlanOpen.value = !editPlanOpen.value
  if (editPlanOpen.value) subtitleOpen.value = false
  if (editPlanOpen.value) await loadEditPlan()
}
async function loadSubtitleTimeline(force = false) {
  if (!hasFinal.value || subtitleLoading.value || (subtitleTimeline.value && !force)) return
  subtitleLoading.value = true
  subtitleError.value = ''
  try {
    const result = await api('GET', '/api/production/' + props.sid + '/subtitles')
    subtitleTimeline.value = result.timeline
    subtitleBaseline.value = subtitleFingerprint(result.timeline)
  } catch (e) { subtitleError.value = (e.body && e.body.error) || e.message }
  finally { subtitleLoading.value = false }
}
async function toggleSubtitleTimeline() {
  subtitleOpen.value = !subtitleOpen.value
  if (subtitleOpen.value) editPlanOpen.value = false
  if (subtitleOpen.value) await loadSubtitleTimeline()
}
async function saveSubtitleTimeline() {
  if (!subtitleTimeline.value || subtitleBusy.value) return
  subtitleBusy.value = true
  subtitleMsg.value = '正在保存…'
  subtitleError.value = ''
  try {
    const result = await api('PUT', '/api/production/' + props.sid + '/subtitles', { timeline: subtitleTimeline.value })
    subtitleTimeline.value = result.timeline
    subtitleBaseline.value = subtitleFingerprint(result.timeline)
    subtitleMsg.value = '字幕文件已保存'
  } catch (e) {
    subtitleError.value = (e.body && e.body.error) || e.message
    subtitleMsg.value = ''
  } finally { subtitleBusy.value = false }
}
async function resetSubtitleTimeline() {
  if (!subtitleTimeline.value || subtitleBusy.value) return
  if (!await confirmModal('恢复生成时的字幕文本和时间？当前字幕文件会归档保留。', { okText: '恢复生成字幕' })) return
  subtitleBusy.value = true
  subtitleMsg.value = '正在恢复…'
  subtitleError.value = ''
  try {
    const result = await api('POST', '/api/production/' + props.sid + '/subtitles/reset', {})
    subtitleTimeline.value = result.timeline
    subtitleBaseline.value = subtitleFingerprint(result.timeline)
    subtitleMsg.value = '已恢复生成字幕'
  } catch (e) { subtitleError.value = (e.body && e.body.error) || e.message }
  finally { subtitleBusy.value = false }
}
function changeEditTransition() {
  if (!editPlan.value) return
  const transition = editPlan.value.transition
  if (transition.type === 'none') transition.duration = 0
  else {
    const current = Number(transition.duration)
    transition.duration = Math.min(
      editPlanTransitionMax.value,
      Number.isFinite(current) && current >= 0.1 ? current : 0.5,
    )
  }
  editPlanMsg.value = ''
}
function moveEditClip(index, offset) {
  if (!editPlan.value) return
  const target = index + offset
  if (target < 0 || target >= editPlan.value.clips.length) return
  const clips = editPlan.value.clips
  const [item] = clips.splice(index, 1)
  clips.splice(target, 0, item)
  clips.forEach((clip, order) => { clip.order = order })
  editPlanMsg.value = ''
}
async function saveEditPlan() {
  if (!editPlan.value || editPlanBusy.value) return null
  editPlanBusy.value = true
  editPlanMsg.value = '正在保存…'
  editPlanError.value = ''
  try {
    const result = await api('PUT', '/api/production/' + props.sid + '/edit-plan', { plan: editPlan.value })
    editPlan.value = result.plan
    editPlanBaseline.value = editPlanFingerprint(result.plan)
    editPlanMsg.value = '剪辑方案已保存'
    return result.plan
  } catch (e) {
    editPlanError.value = (e.body && e.body.error) || e.message
    editPlanMsg.value = ''
    return null
  } finally { editPlanBusy.value = false }
}
async function renderEditPlan() {
  if (!editPlan.value || editPlanBusy.value) return
  const duration = editPlanOutputDuration.value.toFixed(1)
  if (!await confirmModal(`按当前剪辑方案重新合成 ${duration} 秒成片？现有成片会归档保留。`, { okText: '重新合成' })) return
  editPlanBusy.value = true
  editPlanMsg.value = '正在重新合成成片…'
  editPlanError.value = ''
  try {
    const submitted = await api('POST', '/api/production/' + props.sid + '/edit-plan/render', { plan: editPlan.value })
    const finished = submitted.job_id ? await watchJob(submitted.job_id) : { state: 'done', result: submitted }
    if (finished.state === 'failed') throw new Error(finished.error || '重新合成失败')
    mediaRevision.value = Date.now()
    editPlan.value = null
    subtitleTimeline.value = null
    subtitleBaseline.value = ''
    await loadEditPlan(true)
    editPlanMsg.value = '新成片已就绪'
    emit('refresh')
  } catch (e) { editPlanError.value = (e.body && e.body.error) || e.message }
  finally { editPlanBusy.value = false }
}
async function resetEditPlan() {
  if (!editPlan.value || editPlanBusy.value || editPlan.value.source_status !== 'ready') return
  if (!await confirmModal('恢复第一次剪辑前的原始成片？当前成片和剪辑方案会归档保留。', { okText: '恢复原始成片' })) return
  editPlanBusy.value = true
  editPlanMsg.value = '正在恢复原始成片…'
  editPlanError.value = ''
  try {
    const submitted = await api('POST', '/api/production/' + props.sid + '/edit-plan/reset', {})
    const finished = submitted.job_id ? await watchJob(submitted.job_id) : { state: 'done', result: submitted }
    if (finished.state === 'failed') throw new Error(finished.error || '恢复失败')
    mediaRevision.value = Date.now()
    editPlan.value = null
    subtitleTimeline.value = null
    subtitleBaseline.value = ''
    await loadEditPlan(true)
    editPlanMsg.value = '已恢复原始成片'
    emit('refresh')
  } catch (e) { editPlanError.value = (e.body && e.body.error) || e.message }
  finally { editPlanBusy.value = false }
}
async function previewBatchRegeneration() {
  if (!batchSelectedCount.value || batchBusy.value) return null
  batchBusy.value = true
  batchMsg.value = '正在计算影响范围与费用…'
  try {
    const result = await api('POST', '/api/production/' + props.sid + '/regeneration-preview', {
      shots: batchSelection.value.map((entry) => ({ scene_index: entry.scene.scene_index, shot_idx: entry.shot.idx })),
      reason: batchReason.value,
      dimensions: regenDimensions(batchReason.value),
      locked_dimensions: lockedDimensions.value,
    })
    batchPreview.value = result
    batchMsg.value = ''
    return result
  } catch (e) {
    batchMsg.value = '预览失败：' + ((e.body && e.body.error) || e.message)
    return null
  } finally { batchBusy.value = false }
}
async function submitBatchRegeneration() {
  if (!batchSelectedCount.value || batchBusy.value) return
  const preview = batchPreview.value || await previewBatchRegeneration()
  if (!preview) return
  const message = `提交 ${preview.requested_count} 个镜头返工？实际影响 ${preview.affected_count} 个镜头，预计新增费用 ${regenerationEstimateText(preview.cost_estimate)}。`
    + (affectedShotText(preview.affected_shots) ? `\n${affectedShotText(preview.affected_shots)}` : '')
  if (!await confirmModal(message, { okText: '提交批量返工' })) return
  batchBusy.value = true
  batchMsg.value = '正在提交批量返工…'
  try {
    const result = await api('POST', '/api/production/' + props.sid + '/regenerate-shots', {
      shots: preview.requested_shots,
      reason: batchReason.value,
      dimensions: regenDimensions(batchReason.value),
      locked_dimensions: lockedDimensions.value,
    })
    if (result && result.accepted === false) {
      batchMsg.value = '当前已有生成任务，请稍候'
      return
    }
    setBatchMode(false)
    emit('refresh')
  } catch (e) { batchMsg.value = '提交失败：' + ((e.body && e.body.error) || e.message) }
  finally { batchBusy.value = false }
}
watch(() => JSON.stringify({ reason: batchReason.value, locks: lockedDimensions.value }), () => {
  batchPreview.value = null
})
function shotMetric(sc, shot) {
  return (metrics.value.shots || []).find((item) =>
    Number(item.scene_index) === Number(sc.scene_index) && Number(item.shot_index) === Number(shot.idx)) || null
}
function productionFact(sc, shot) {
  const metric = shotMetric(sc, shot)
  return metric && metric.current_generation ? metric.current_generation : null
}
function formatSeconds(value) {
  if (value == null || value === '') return '—'
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return '—'
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}
function formatRate(value) {
  if (value == null || value === '') return '待确认'
  const rate = Number(value)
  return Number.isFinite(rate) ? `${Math.round(rate * 100)}%` : '待确认'
}
function money(value, currency) {
  if (value == null || value === '') return '—'
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'
  const symbol = currency === 'CNY' ? '¥' : currency === 'USD' ? '$' : ''
  return `${symbol}${amount.toFixed(2)}`
}
function hasActualCost(cost) {
  if (!cost) return false
  return Number(cost.actual_record_count ?? (cost.actual_cost == null ? 0 : 1)) > 0
}
function actualCostText(cost) {
  if (!hasActualCost(cost)) return ''
  const actual = Number(cost.actual_total ?? cost.actual_cost)
  return Number.isFinite(actual) ? money(actual, cost.currency) : ''
}
function routeText(record) {
  const route = (record && record.route) || {}
  return [route.provider_id, route.model_id].filter(Boolean).join(' · ') || '未记录模型'
}
function scriptSceneText(scene) {
  if (typeof scene === 'string') return scene
  if (!scene || typeof scene !== 'object') return ''
  return scene.script || scene.content || scene.text || scene.description || ''
}
const scriptText = computed(() => {
  const story = String(script.value.story || '').trim()
  if (story) return story
  return (script.value.scenes || []).map(scriptSceneText).map((text) => String(text).trim()).filter(Boolean).join('\n\n')
})
const storyBlocks = computed(() => scriptText.value.split(/\n{2,}/).map((text) => text.trim()).filter((text) => text && text !== '---').map((text) => {
  const heading = /^#{1,6}\s+/.test(text) || /^\*\*[^*]+\*\*$/.test(text)
  return { heading, text: text.replace(/^#{1,6}\s+/, '').replace(/^\*\*|\*\*$/g, '').replace(/\*\*/g, '') }
}))
async function startScriptEdit() {
  scriptBaseline.value = scriptText.value
  scriptDraft.value = scriptText.value
  scriptMsg.value = ''
  scriptEditing.value = true
  await nextTick()
  if (scriptEditorEl.value) {
    scriptEditorEl.value.textContent = scriptDraft.value
    scriptEditorEl.value.focus()
  }
}
function onScriptInput(event) {
  scriptDraft.value = String(event.currentTarget.innerText || '').replace(/\u00a0/g, ' ')
}
async function cancelScriptEdit() {
  if (scriptDirty.value && !await confirmModal('放弃尚未保存的剧本修改？', { okText: '放弃修改', danger: true })) return
  scriptEditing.value = false
  scriptDraft.value = scriptBaseline.value
  scriptMsg.value = ''
}
async function saveScript() {
  const text = scriptDraft.value.trim()
  if (!text) { scriptMsg.value = '剧本正文不能为空'; return }
  if (!scriptDirty.value || scriptSaving.value) return
  scriptSaving.value = true
  scriptMsg.value = '保存中…'
  try {
    const result = await api('PUT', '/api/production/' + props.sid + '/script', { text })
    if (result && result.ok === false) { scriptMsg.value = '失败：' + (result.note || result.error || ''); return }
    scriptEditing.value = false
    scriptBaseline.value = text
    scriptDraft.value = text
    scriptMsg.value = '已保存'
    await load()
    emit('refresh')
  } catch (e) { scriptMsg.value = '失败：' + ((e.body && (e.body.note || e.body.error)) || e.message) }
  finally { scriptSaving.value = false }
}
const qualityTierLabel = computed(() => ({ economy: '省钱', balanced: '均衡', quality: '高质量' })[props.snap && props.snap.quality_tier] || '均衡')
const sceneNames = computed(() => (script.value.scenes || []).map((scene) => {
  const match = String(scene).match(/【场景：([^】]+)】/)
  return match ? match[1] : ''
}).filter(Boolean))
const totals = computed(() => {
  let total = 0, frames = 0, videos = 0
  ;(sb.value.scenes || []).forEach((sc) => (sc.shots || []).forEach((shot) => {
    total++
    const m = shotMedia(sc, shot.idx)
    if (m['video.mp4']) { videos++; frames++ } else if (m['first_frame.png']) frames++
  }))
  return { total, frames, videos, pct: total ? Math.round((videos / total) * 100) : 0 }
})
function shotState(sc, shot) {
  const m = shotMedia(sc, shot.idx)
  if (m['video.mp4']) return { k: 'done', video: imgFor(m['video.mp4']) }
  if (m['first_frame.png']) return { k: 'kf', img: imgFor(m['first_frame.png']) }
  return { k: 'pending' }
}

// 把镜头/已完成/场景数报给上层（Hero 概览常显指标）
watch(totals, (t) => emit('stats', {
  shots: t.total,
  frames: t.frames,
  videos: t.videos,
  scenes: (sb.value.scenes || []).length,
}), { immediate: true })

// 时长从视频元数据读取（后端清单不带时长），加载后显示在卡片角上
const durations = ref({})
const history = reactive({})
const joinList = (value) => Array.isArray(value) ? value.join(', ') : String(value || '')
const splitList = (value) => String(value || '').split(/[,，;；\n]/).map((x) => x.trim()).filter(Boolean)
function onMeta(e, key) { const d = e.target && e.target.duration; if (d && isFinite(d)) durations.value[key] = d }
function fmtDur(d) {
  if (!d || !isFinite(d)) return ''
  if (d < 60) return d.toFixed(1) + 's'
  const m = Math.floor(d / 60), s = Math.round(d % 60)
  return m + ':' + String(s).padStart(2, '0')
}
const imgFor = (key) => mediaUrl('/api/production/' + props.sid + '/file?path=' + encodeURIComponent(key) + '&v=' + mediaRevision.value)
function shotEntry(sc, idx) { return ((mediaByScene.value[sc.scene_index] || { shots: [] }).shots || []).find((x) => x.idx === idx) || {} }
function shotMedia(sc, idx) { return shotEntry(sc, idx).media || {} }
function promptPreflightBadge(sc, shot) {
  const report = shotEntry(sc, shot.idx).prompt_preflight
  if (!report || !report.status) return null
  const labels = {
    passed: { text: '提示词检查通过', cls: 'q-ok' },
    rewritten: { text: '提示词已自动修正', cls: 'q-warn' },
    review: { text: '提示词需要复核', cls: 'q-warn' },
    blocked: { text: '提示词存在冲突', cls: 'q-bad' },
  }
  const badge = labels[report.status] || { text: report.status, cls: 'q-warn' }
  const reasons = (report.issues || []).map((issue) => issue.message || issue.code).filter(Boolean)
  return { ...badge, title: reasons.join('\n') || badge.text }
}
function continuityState(sc, shot) {
  return shotEntry(sc, shot.idx).continuity || null
}
function continuityBadge(sc, shot) {
  const memory = continuityState(sc, shot)
  if (!memory) return null
  const assets = memory.invalidation_keys || []
  const dependencies = memory.depends_on_shot_idxs || []
  const invalidations = memory.invalidations || []
  const repairs = memory.repair_suggestions || []
  const lines = []
  if (assets.length) lines.push('绑定资产：' + assets.map((item) => String(item).split(':').slice(1).join(':')).join('、'))
  if (dependencies.length) lines.push('继承镜头：' + dependencies.map((item) => Number(item) + 1).join('、'))
  if ((memory.transitions || []).length) lines.push('状态变化：' + memory.transitions.map((item) => item.kind).filter(Boolean).join('、'))
  if (memory.inherited_from && memory.inherited_from.source_session_id) {
    lines.push('延续项目：' + memory.inherited_from.source_session_id)
  }
  if (invalidations.length) {
    const changed = [...new Set(invalidations.map((item) => item.asset_id).filter(Boolean))]
    lines.push('已变更资产：' + changed.join('、'))
    return { text: '资产已变更 ' + changed.length, cls: 'q-bad', title: lines.join('\n') }
  }
  if (repairs.length) {
    lines.push('修复建议：' + repairs.map((item) => item.message).filter(Boolean).join('；'))
    return { text: '连续性待核对 ' + repairs.length, cls: 'q-warn', title: lines.join('\n') }
  }
  return { text: '连续性 ' + (assets.length || '已记录'), cls: 'q-info', title: lines.join('\n') || '镜头状态已写入连续性账本' }
}
function continuityRepairs(sc, shot) {
  return (continuityState(sc, shot) || {}).repair_suggestions || []
}
function candidateBadge(sc, shot) {
  const selection = (shotEntry(sc, shot.idx).render_plan || {}).candidate_selection
  const total = Number(selection && selection.candidate_count)
  const selected = Number(selection && selection.selected_candidate)
  if (!Number.isFinite(total) || total < 2 || !Number.isFinite(selected)) return null
  const candidates = Array.isArray(selection.candidates) ? selection.candidates : []
  const scores = candidates.map((item) => {
    const index = Number(item.candidate_index)
    const score = Number(item.score)
    return Number.isFinite(index) && Number.isFinite(score) ? `候选 ${index}: ${score.toFixed(2)}` : ''
  }).filter(Boolean)
  const failed = Array.isArray(selection.errors) ? selection.errors.length : 0
  const details = [`已从 ${total} 个候选中保留第 ${selected} 个`, ...scores]
  if (failed) details.push(`${failed} 个候选生成失败`)
  return { text: `${total}选1 · #${selected}`, title: details.join('\n') }
}
function durationAdjustment(sc, shot) {
  const plan = shotEntry(sc, shot.idx).render_plan
  if (!plan || plan.requested_duration_sec == null) return null
  const planned = Number(plan.planned_duration_sec)
  const requested = Number(plan.requested_duration_sec)
  if (!Number.isFinite(requested) || plan.exact === true || (Number.isFinite(planned) && Math.abs(planned - requested) < 0.01)) return null
  const reason = plan.reason === 'backend_fixed' ? '该后端使用固定时长' : '已选择最接近的可用时长'
  const provider = plan.provider ? `（${plan.provider}）` : ''
  return {
    text: `后端 ${requested.toFixed(1)}s`,
    title: `计划 ${planned.toFixed(1)}s，实际请求 ${requested.toFixed(1)}s${provider}。${reason}。`,
  }
}

async function load() {
  loading.value = true; err.value = ''
  try {
    const [sc, s2, mn, mt] = await Promise.all([
      api('GET', '/api/production/' + props.sid + '/script'),
      api('GET', '/api/production/' + props.sid + '/storyboard'),
      api('GET', '/api/production/' + props.sid + '/artifacts'),
      api('GET', '/api/production/' + props.sid + '/metrics'),
    ])
    script.value = sc; sb.value = s2; man.value = mn; metrics.value = mt
    editScenes.value = (s2.scenes || []).map((scn) => ({
      scene_index: scn.scene_index,
      shots: (scn.shots || []).map((sh) => ({
        duration_sec: Number(sh.duration_sec) || 5, director_desc: sh.director_desc || '',
        beats: (sh.beats || []).map((beat) => ({ ...beat })),
        visual_desc: sh.visual_desc || '', audio_desc: localizeAudioTags(sh.audio_desc),
        visual_style_text: joinList(sh.visual_style), avoid_text: joinList(sh.avoid),
        screen_text: sh.screen_text || '', screen_text_pos: sh.screen_text_pos || '',
      })),
    }))
    storyboardBaseline.value = storyboardFingerprint(editScenes.value)
    sbMsg.value = ''
  } catch (e) { err.value = e.message }
  loading.value = false
}
onMounted(load)
watch(() => props.sid, () => {
  Object.keys(history).forEach((key) => delete history[key])
  scriptEditing.value = false
  scriptDraft.value = ''
  scriptBaseline.value = ''
  scriptMsg.value = ''
  setBatchMode(false)
  mediaRevision.value = Date.now()
  editPlan.value = null
  editPlanBaseline.value = ''
  editPlanOpen.value = false
  subtitleTimeline.value = null
  subtitleBaseline.value = ''
  subtitleOpen.value = false
  load()
})

// While generating, the shot videos land on disk one by one. Re-fetch just the
// manifest on an interval so each finished 分镜视频 pops into the gallery live
// (生成一个展示一个), without waiting for the whole stage to complete.
let pollTimer = null
async function refetchManifest() {
  try {
    const [manifest, productionMetrics] = await Promise.all([
      api('GET', '/api/production/' + props.sid + '/artifacts'),
      api('GET', '/api/production/' + props.sid + '/metrics'),
    ])
    man.value = manifest
    metrics.value = productionMetrics
  } catch (e) { /* ignore */ }
}
function syncPoll(busy) {
  if (busy && !pollTimer) pollTimer = setInterval(refetchManifest, 4000)
  else if (!busy && pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
watch(() => !!(props.snap && props.snap.busy), syncPoll, { immediate: true })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

async function save() {
  if (!storyboardDirty.value || sbMsg.value === '保存中…') return
  const scenes = editScenes.value.map((sc) => ({
    scene_index: sc.scene_index,
    shots: sc.shots.map((sh) => ({
      duration_sec: Number(sh.duration_sec) || 5, director_desc: (sh.director_desc || '').trim(),
      beats: (sh.beats || []).map((beat) => ({
        start_sec: Number(beat.start_sec) || 0, end_sec: Number(beat.end_sec) || 0,
        action: (beat.action || '').trim(), performance: (beat.performance || '').trim(), camera: (beat.camera || '').trim(),
      })),
      visual_desc: (sh.visual_desc || '').trim(), audio_desc: normalizeAudioTags((sh.audio_desc || '').trim()),
      visual_style: splitList(sh.visual_style_text), avoid: splitList(sh.avoid_text),
      screen_text: (sh.screen_text || '').trim(), screen_text_pos: sh.screen_text_pos || null,
    })),
  }))
  if (!await confirmModal('保存全部分镜修改？已生成的画面/镜头视频将作废，下游会按新分镜重新生成。', { okText: '保存', danger: true })) return
  sbMsg.value = '保存中…'
  try {
    const r = await api('PUT', '/api/production/' + props.sid + '/storyboard', { scenes })
    if (r && r.ok === false) { sbMsg.value = '失败：' + (r.note || r.error || ''); return }
    storyboardBaseline.value = storyboardFingerprint(editScenes.value)
    sbMsg.value = '已保存'
    emit('refresh')
  } catch (e) { sbMsg.value = '失败：' + ((e.body && (e.body.note || e.body.error)) || e.message) }
}

async function generateStoryboardKeyframe({ sceneIndex, shotIndex, force }) {
  if (storyboardDirty.value || (props.snap && props.snap.busy)) return
  if (force && !await confirmModal(
    '重新生成场景 ' + (sceneIndex + 1) + ' · 镜 ' + (shotIndex + 1) + ' 的首帧？当前首帧会保留在历史版本中。',
    { okText: '重新生成' },
  )) return
  sbMsg.value = force ? '正在提交首帧重生成任务…' : '正在提交首帧生成任务…'
  try {
    const result = await api('POST', '/api/production/' + props.sid + '/preview-keyframes', {
      scene_index: sceneIndex,
      shot_index: shotIndex,
      force: !!force,
    })
    if (result && result.ok === false) {
      sbMsg.value = '失败：' + (result.note || result.error || '首帧任务未启动')
      return
    }
    sbMsg.value = force ? '首帧重生成任务已提交' : '首帧生成任务已提交'
    emit('refresh')
  } catch (e) {
    sbMsg.value = '首帧生成失败：' + ((e.body && (e.body.note || e.body.error)) || e.message)
  }
}

async function refreshStoryboardAssets() {
  mediaRevision.value = Date.now()
  await load()
  emit('refresh')
}

async function loadRegenerationImpact(sceneIdx, shotIdx, dimensions = ['visual']) {
  try {
    return await api('POST', '/api/production/' + props.sid + '/regeneration-impact', {
      shot_idx: shotIdx, scene_index: sceneIdx, dimensions,
    })
  } catch (e) { return null }
}

function impactText(impact) {
  const affected = (impact && impact.affected_shots || []).map((item) => Number(item.shot_idx) + 1)
  if (!affected.length) return '将只重生成当前镜头。'
  if (affected.length === 1) return '连续性账本确认：只重生成当前镜头。'
  return '连续性账本确认：镜 ' + affected.join('、') + ' 会一并重生成。'
}

async function regen(sceneIdx, shotIdx) {
  const reason = regenReason(sceneIdx, shotIdx)
  const impact = await loadRegenerationImpact(sceneIdx, shotIdx, regenDimensions(reason))
  const message = '重生成 场景' + (Number(sceneIdx) + 1) + ' · 镜 ' + (Number(shotIdx) + 1)
    + '？' + impactText(impact) + '预计新增费用 ' + regenerationEstimateText(impact && impact.cost_estimate)
    + '。完成后会重新合成成片，剧本和分镜不变。'
  if (!await confirmModal(message, { okText: '重生成' })) return
  try {
    await api('POST', '/api/production/' + props.sid + '/regenerate-shot', {
      shot_idx: shotIdx,
      scene_index: sceneIdx,
      reason,
      dimensions: regenDimensions(reason),
      locked_dimensions: lockedDimensions.value,
    })
    emit('refresh')
  }
  catch (e) { sbMsg.value = '重生成失败：' + ((e.body && e.body.error) || e.message) }
}

// 改提示词重生成：在原展示位置编辑导演稿、画面和台词，提交后重抽该镜。
const edit = reactive({})
const ekey = (sc, shot) => sc.scene_index + '_' + shot.idx
function promptEditState(sc, shot) { return edit[ekey(sc, shot)] || null }
function isPromptEditing(sc, shot) { const state = promptEditState(sc, shot); return !!(state && state.open) }
function openEdit(sc, shot) {
  const visibleVisual = reviewableVisualPrompt(shot.visual_desc, shot.director_desc)
  edit[ekey(sc, shot)] = {
    open: true,
    dd: shot.director_desc || '',
    vd: visibleVisual,
    rawVd: shot.visual_desc || '',
    ad: localizeAudioTags(shot.audio_desc),
    original: {
      dd: shot.director_desc || '',
      vd: visibleVisual,
      ad: localizeAudioTags(shot.audio_desc),
    },
    busy: false,
    msg: '',
  }
}
function shotPromptDirty(sc, shot) {
  const state = promptEditState(sc, shot)
  if (!state || !state.original) return false
  return state.dd !== state.original.dd || state.vd !== state.original.vd || state.ad !== state.original.ad
}
async function cancelEdit(sc, shot) {
  const state = promptEditState(sc, shot)
  if (!state) return
  if (shotPromptDirty(sc, shot) && !await confirmModal('放弃尚未保存的提示词修改？', { okText: '放弃修改', danger: true })) return
  state.open = false
  state.msg = ''
}
async function submitEdit(sc, shot) {
  const e = edit[ekey(sc, shot)]; if (!e) return
  if (!(e.vd || '').trim()) { e.msg = '画面描述不能为空'; return }
  if (!shotPromptDirty(sc, shot) || e.busy) return
  const dimensions = []
  if (e.dd !== e.original.dd || e.vd !== e.original.vd) dimensions.push('visual')
  if (e.ad !== e.original.ad) dimensions.push('audio')
  const impact = await loadRegenerationImpact(sc.scene_index, shot.idx, dimensions)
  if (!await confirmModal('保存提示词并重生成镜 ' + (Number(shot.idx) + 1) + '？' + impactText(impact), { okText: '保存并重生成' })) return
  e.busy = true; e.msg = '提交中…（用新提示词重生成）'
  try {
    const visualDesc = e.vd === e.original.vd ? e.rawVd : e.vd
    await api('POST', '/api/production/' + props.sid + '/regenerate-shot',
      { shot_idx: shot.idx, scene_index: sc.scene_index, director_desc: e.dd, visual_desc: visualDesc, audio_desc: normalizeAudioTags(e.ad), reason: 'prompt_edit', dimensions })
    e.open = false; emit('refresh')
  } catch (err) { e.msg = '失败：' + ((err.body && err.body.error) || err.message) }
  e.busy = false
}

function historyState(sc, shot, scope = 'generation') {
  const key = scope + ':' + ekey(sc, shot)
  if (!history[key]) history[key] = {
    open: true,
    loading: false,
    loaded: false,
    groups: [],
    activeType: scope === 'storyboard' ? 'keyframe' : 'video',
    compare: null,
    annotationTarget: null,
    annotations: [],
    annotationLoading: false,
    annotationText: '',
    annotationTime: '',
    annotationSaving: false,
    annotationError: '',
    error: '',
  }
  return history[key]
}
const versionTypeLabel = (type) => ({ storyboard: '分镜', keyframe: '首帧', video: '视频' }[type] || type)
const versionStatusLabel = (status) => ({ active: '当前', stale: '已过期', archived: '历史' }[status] || status)
function fmtVersionTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}
const versionUrl = (item) => mediaUrl('/api/production/' + props.sid + '/artifact-versions/' + item.artifact_id + '/file')
async function loadHistory(sc, shot, scope = 'generation', force = false) {
  const state = historyState(sc, shot, scope)
  if (state.loading || (state.loaded && !force)) return
  state.loading = true; state.error = ''
  try {
    const types = scope === 'storyboard' ? ['keyframe'] : ['video']
    const groups = await Promise.all(types.map(async (type) => {
      const query = '?scene_index=' + sc.scene_index + '&shot_index=' + shot.idx + '&artifact_type=' + type
      const result = await api('GET', '/api/production/' + props.sid + '/artifact-versions' + query)
      const versions = result.versions || []
      return {
        type,
        versions: versions.map((item, index) => ({
          ...item,
          display_version: versions.length - index,
        })),
      }
    }))
    state.groups = groups.filter((group) => group.versions.length)
    if (state.compare) {
      state.compare = state.groups.flatMap((group) => group.versions)
        .find((item) => item.artifact_id === state.compare.artifact_id) || null
    }
    if (!state.groups.some((group) => group.type === state.activeType)) {
      state.activeType = state.groups[0] ? state.groups[0].type : (scope === 'storyboard' ? 'keyframe' : 'video')
    }
    state.loaded = true
  } catch (e) {
    state.error = e.status === 409 ? '当前项目尚未启用版本记录' : ('加载失败：' + e.message)
    state.loaded = true
  }
  state.loading = false
}
function activeHistoryGroup(sc, shot, scope = 'generation') {
  const state = historyState(sc, shot, scope)
  return state.groups.find((group) => group.type === state.activeType) || state.groups[0] || null
}
function compareVersion(sc, shot, item) {
  historyState(sc, shot).compare = item
}
function clearVersionComparison(sc, shot) {
  historyState(sc, shot).compare = null
}
function formatAnnotationTimecode(value) {
  if (value == null || value === '') return ''
  const seconds = Math.max(0, Number(value))
  if (!Number.isFinite(seconds)) return ''
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${(seconds % 60).toFixed(1).padStart(4, '0')}`
}
async function openVersionAnnotation(sc, shot, item) {
  const state = historyState(sc, shot)
  state.annotationTarget = item
  state.annotationText = ''
  state.annotationTime = ''
  state.annotationError = ''
  state.annotationLoading = true
  try {
    const result = await api('GET', '/api/production/' + props.sid + '/artifact-versions/' + item.artifact_id + '/annotations')
    state.annotations = result.annotations || []
  } catch (e) { state.annotationError = '批注加载失败：' + ((e.body && e.body.error) || e.message) }
  finally { state.annotationLoading = false }
}
function closeVersionAnnotation(sc, shot) {
  const state = historyState(sc, shot)
  state.annotationTarget = null
  state.annotations = []
  state.annotationError = ''
}
async function saveVersionAnnotation(sc, shot) {
  const state = historyState(sc, shot)
  if (!state.annotationTarget || !state.annotationText.trim() || state.annotationSaving) return
  state.annotationSaving = true
  state.annotationError = ''
  try {
    const result = await api('POST', '/api/production/' + props.sid + '/artifact-versions/' + state.annotationTarget.artifact_id + '/annotations', {
      text: state.annotationText.trim(),
      timecode_seconds: state.annotationTime === '' ? null : Number(state.annotationTime),
    })
    state.annotations.push(result.annotation)
    state.annotationText = ''
    state.annotationTime = ''
  } catch (e) { state.annotationError = '保存失败：' + ((e.body && e.body.error) || e.message) }
  finally { state.annotationSaving = false }
}
async function toggleHistory(sc, shot, scope = 'generation') {
  const state = historyState(sc, shot, scope)
  state.open = !state.open
  if (state.open) await loadHistory(sc, shot, scope)
}
async function selectVersion(sc, shot, item, scope = 'generation') {
  if (!await confirmModal('使用' + versionTypeLabel(item.artifact_type) + ' v' + (item.display_version || item.version) + '？当前版本仍会保留，可随时重新选择。', { okText: '使用此版本' })) return
  const state = historyState(sc, shot, scope)
  try {
    await api('POST', '/api/production/' + props.sid + '/artifact-versions/' + item.artifact_id + '/rollback')
    mediaRevision.value = Date.now()
    await refetchManifest()
    await loadHistory(sc, shot, scope, true)
    state.compare = null
  } catch (e) { state.error = '切换失败：' + ((e.body && e.body.error) || e.message) }
}
watch(() => {
  const entry = selectedGeneration.value
  return `${props.view}:${entry ? entry.key + ':' + shotState(entry.scene, entry.shot).k : ''}`
}, () => {
  const entry = selectedGeneration.value
  if (props.view !== 'shot_video' || !entry || shotState(entry.scene, entry.shot).k !== 'done') return
  const state = historyState(entry.scene, entry.shot)
  state.open = true
  loadHistory(entry.scene, entry.shot)
}, { immediate: true })
</script>

<template>
  <div class="review-content">
    <div v-if="loading" class="muted">加载审核内容…</div>
    <div v-else-if="err" class="muted">内容加载失败：{{ err }}</div>
    <template v-else>
      <!-- 当前阶段内容的「修改」（针对本内容，按意见让 AI 重做） -->
      <div v-if="canReviseHere && view !== 'script'" class="rev-inline" :class="{ expanded: reviseOpen }">
        <button v-if="!reviseOpen" class="ghost" @click="reviseOpen = true"><Pencil :size="15" />修改{{ reviseLabel }}</button>
        <div v-else class="revbox" style="margin-top:0">
          <label style="font-weight:600">修改{{ reviseLabel }}</label>
          <textarea v-model="reviseText" placeholder="例：台词更口语一点；删掉第3场；第2镜画面太暗，重生成……"
            @keydown.enter.ctrl="submitRevise" @keydown.enter.meta="submitRevise" @keydown.esc="reviseOpen = false"></textarea>
          <div class="row" style="margin-top:8px"><button class="act" @click="submitRevise">提交修改</button><button class="ghost" @click="reviseOpen = false">取消</button> <span class="muted">{{ reviseMsg }}</span></div>
        </div>
      </div>

      <!-- 剧本 -->
      <div v-if="view === 'script'" class="review-stage">
        <div class="stage-heading">
          <div><h2>剧本创作</h2><span>{{ (script.scenes || []).length }} 个场景 · {{ storyboardEntries.length }} 个镜头 · {{ totalDuration.toFixed(1) }} 秒</span></div>
          <button v-if="canRequestScriptEdit" class="ghost" type="button" @click="emit('reopen', 'script')"><Pencil :size="15" />编辑剧本</button>
        </div>
        <div v-if="scriptText" class="content-review-layout">
          <article class="script-document" :class="{ editing: scriptEditing }">
            <div v-if="scriptEditable" class="script-revise-tools">
              <button v-if="!scriptEditing" class="ghost" type="button" @click="startScriptEdit"><Pencil :size="15" />编辑剧本</button>
              <div v-else class="script-edit-actions">
                <span v-if="scriptDirty" class="inline-dirty"><AlertTriangle :size="13" />有未保存的修改</span>
                <span v-else class="muted">尚未修改</span>
                <button class="act" type="button" :disabled="scriptSaving || !scriptDirty" @click="saveScript"><Save :size="14" />{{ scriptSaving ? '保存中…' : '保存剧本' }}</button>
                <button class="ghost" type="button" :disabled="scriptSaving" @click="cancelScriptEdit">取消</button>
              </div>
            </div>
            <div v-if="scriptEditing" ref="scriptEditorEl" class="script-editor-surface" contenteditable="plaintext-only"
              role="textbox" aria-label="剧本正文" aria-multiline="true" spellcheck="false" @input="onScriptInput"></div>
            <template v-else>
              <template v-for="(block, index) in storyBlocks" :key="index">
                <h3 v-if="block.heading">{{ block.text }}</h3>
                <p v-else>{{ block.text }}</p>
              </template>
            </template>
            <div v-if="scriptMsg" class="script-save-message" :class="{ error: scriptMsg.startsWith('失败') }" role="status">{{ scriptMsg }}</div>
          </article>
          <aside class="context-inspector">
            <section>
              <h3>本片设置</h3>
              <div class="context-row"><Film :size="16" /><div><strong>{{ qualityTierLabel }}</strong><span>质量档位</span></div></div>
              <div class="context-row"><Clapperboard :size="16" /><div><strong>{{ storyboardEntries.length || '—' }} 个镜头</strong><span>{{ totalDuration ? totalDuration.toFixed(1) + ' 秒' : '等待分镜' }}</span></div></div>
            </section>
            <section v-if="(script.characters || []).length">
              <h3>角色</h3>
              <div v-for="character in script.characters" :key="character.idx" class="context-row">
                <UserRound :size="16" /><div><strong>{{ character.identifier_in_scene }}</strong><span>{{ character.dynamic_features || character.static_features }}</span></div>
              </div>
            </section>
            <section v-if="sceneNames.length">
              <h3>场景</h3>
              <div v-for="name in sceneNames" :key="name" class="context-row"><MapPin :size="16" /><div><strong>{{ name }}</strong><span>场景模型约束已进入分镜</span></div></div>
            </section>
            <section v-if="(snap && snap.loras || []).length">
              <h3>LoRA</h3>
              <div v-for="item in snap.loras" :key="item.lora_id" class="context-row"><Boxes :size="16" /><div><strong>{{ item.display_name }}</strong><span>{{ item.application_mode === 'native' ? '原生 LoRA' : '触发词兼容' }} · 权重 {{ item.default_weight }}</span></div></div>
            </section>
          </aside>
        </div>
        <div v-if="!scriptText" class="muted">剧本尚未生成。</div>
      </div>

      <!-- 分镜脚本 -->
      <div v-else-if="view === 'storyboard'" class="review-stage">
        <div class="stage-heading">
          <div><h2>分镜设计</h2><span>{{ (sb.scenes || []).length }} 个场景 · {{ storyboardEntries.length }} 个镜头 · {{ totalDuration.toFixed(1) }} 秒</span></div>
          <button v-if="canRequestStoryboardEdit" class="ghost" type="button" @click="emit('reopen', 'storyboard')"><Pencil :size="15" />编辑分镜</button>
        </div>
        <div v-if="!hasStoryboard" class="muted">分镜脚本尚未生成。</div>
        <StoryboardEditor v-else v-model="editScenes" :sid="sid" :dirty="storyboardDirty"
          :saving="sbMsg === '保存中…'" :save-message="sbMsg" :read-only="!sbEditable"
          :media-by-scene="mediaByScene" :media-revision="mediaRevision" :busy="!!(snap && snap.busy)"
          @save="save" @generate-keyframe="generateStoryboardKeyframe" @refresh="refreshStoryboardAssets" />
      </div>

      <!-- 分镜视频：镜头看板 -->
      <div v-else-if="view === 'shot_video'" class="review-stage">
        <div v-if="!showBoard" class="muted">分镜视频尚未开始生成。</div>
        <template v-else>
          <section class="shot-overview" aria-label="镜头概览">
            <div class="shot-overview-head">
              <div class="shot-overview-title">
                <h2>镜头生成</h2>
                <span>{{ totals.videos }}/{{ totals.total }} 已完成 · {{ qualityIssueCount ? qualityIssueCount + ' 个需要复核' : (metricsSummary.quality_evaluated_shots ? '质量检查通过' : '等待质量检查') }}</span>
              </div>
              <div class="shot-overview-actions">
                <button v-if="canCost" class="ghost" type="button" title="查看成本估算" @click="emit('cost')"><Calculator :size="14" />成本估算</button>
                <button v-if="!batchMode" class="ghost" type="button" title="选择多个镜头进行处理" :disabled="snap && snap.busy" @click="setBatchMode(true)"><CheckSquare :size="14" />批量</button>
              </div>
            </div>
            <span v-if="totals.pct < 100" class="stage-progress shot-overview-progress"><i :style="{ width: totals.pct + '%' }"></i></span>
            <div v-if="metricsSummary.generated_shots" class="shot-overview-metrics" aria-label="生产指标">
              <div v-if="metricsSummary.quality_evaluated_shots"><strong>{{ formatRate(metricsSummary.quality_pass_rate) }}</strong><span>质量通过</span><small>{{ metricsSummary.quality_passed_shots || 0 }}/{{ metricsSummary.quality_evaluated_shots || 0 }} 已检查</small></div>
              <div><strong>{{ metricsSummary.total_reworks || 0 }}</strong><span>返工次数</span><small>平均 {{ Number(metricsSummary.mean_reworks_per_generated_shot || 0).toFixed(1) }} 次/镜</small></div>
              <div v-if="metricsSummary.mean_generation_seconds != null"><strong>{{ formatSeconds(metricsSummary.mean_generation_seconds) }}</strong><span>平均生成</span><small>排队 {{ formatSeconds(metricsSummary.mean_queue_seconds) }}</small></div>
              <div v-if="hasActualCost(metricCost)"><strong>{{ actualCostText(metricCost) }}</strong><span>实际费用</span><small>账单覆盖 {{ formatRate(metricCost.actual_coverage_rate) }}</small></div>
            </div>
            <p v-if="cumulativeSavingsText(reworkSavings)" class="rework-savings-rollup">{{ cumulativeSavingsText(reworkSavings) }}</p>
          </section>
          <section v-if="batchMode" class="batch-review-bar" aria-label="批量处理镜头">
            <div class="batch-review-primary">
              <div class="batch-review-summary"><strong>{{ batchSelectedCount ? `已选 ${batchSelectedCount} 个镜头` : '选择要重新生成的镜头' }}</strong><span>{{ batchSelectedCount ? '提交前会确认影响范围和费用' : '勾选下方镜头卡片' }}</span></div>
              <div class="batch-review-actions">
                <button class="act" type="button" :disabled="!batchSelectedCount || batchBusy || (snap && snap.busy)" @click="submitBatchRegeneration"><RefreshCw :size="14" />重新生成</button>
                <button class="ghost batch-icon-action" type="button" :class="{ active: batchSettingsOpen }" title="返工设置" aria-label="返工设置" @click="batchSettingsOpen = !batchSettingsOpen"><SlidersHorizontal :size="15" /></button>
                <button class="ghost batch-icon-action" type="button" title="退出批量处理" aria-label="退出批量处理" @click="setBatchMode(false)"><X :size="15" /></button>
              </div>
            </div>
            <div v-if="batchSettingsOpen" class="batch-review-settings">
              <label class="regen-reason"><span>原因</span><select v-model="batchReason"><option v-for="option in regenReasonOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
              <div class="dimension-locks" aria-label="返工锁定约束"><span><Lock :size="13" />保持不变</span><label><input v-model="reworkLocks.identity" type="checkbox" />人物</label><label><input v-model="reworkLocks.composition" type="checkbox" />构图</label><label><input v-model="reworkLocks.motion" type="checkbox" />动作</label><label><input v-model="reworkLocks.audio" type="checkbox" />声音</label></div>
            </div>
            <div v-if="batchPreview" class="batch-impact-inline">预计影响 {{ batchPreview.affected_count }} 个镜头 · 新增 {{ regenerationEstimateText(batchPreview.cost_estimate) }}</div>
            <div v-if="batchMsg" class="batch-rework-message">{{ batchMsg }}</div>
          </section>
          <div class="generation-layout generation-stacked">
            <main class="generation-scenes">
              <section v-for="sc in sb.scenes" :key="sc.scene_index" class="generation-scene-block">
                <div class="shot-list-scene">场景 {{ Number(sc.scene_index) + 1 }}</div>
                <div class="board">
                  <article v-for="shot in (sc.shots || [])" :key="shot.idx" class="cshot" :class="{ selected: selectedGenerationKey === sc.scene_index + '_' + shot.idx, 'batch-selected': !!batchSelected[sc.scene_index + '_' + shot.idx] }">
                    <label v-if="batchMode" class="batch-shot-check" @click.stop><input type="checkbox" :checked="!!batchSelected[sc.scene_index + '_' + shot.idx]" :aria-label="'选择场景 ' + (Number(sc.scene_index) + 1) + ' 镜 ' + (Number(shot.idx) + 1)" @change="toggleBatchShot(sc.scene_index, shot.idx, $event.target.checked)" /><span><Check :size="13" /></span></label>
                    <div class="cframe" :class="{ pending: shotState(sc, shot).k === 'pending', kf: shotState(sc, shot).k === 'kf' }">
                      <template v-if="shotState(sc, shot).k === 'done'">
                        <span class="statetag st-done">已完成</span>
                        <span v-if="durations[sc.scene_index + '_' + shot.idx]" class="dur">{{ fmtDur(durations[sc.scene_index + '_' + shot.idx]) }}</span>
                        <video controls preload="metadata" :src="shotState(sc, shot).video" @loadedmetadata="onMeta($event, sc.scene_index + '_' + shot.idx)"></video>
                      </template>
                      <template v-else-if="shotState(sc, shot).k === 'kf'"><span class="statetag st-kf">关键帧</span><img :src="shotState(sc, shot).img" /></template>
                      <template v-else><span class="statetag st-pend">待生成</span><Clapperboard :size="24" /></template>
                    </div>
                    <button type="button" class="gen-card-select" @click="selectedGenerationKey = sc.scene_index + '_' + shot.idx">
                      <span><strong>镜 {{ shot.idx + 1 }}</strong><small>{{ Number(shot.duration_sec || 5).toFixed(1) }}s</small></span>
                      <span class="gen-card-badges">
                        <span v-if="qualityBadge(shotEntry(sc, shot.idx).quality)" class="qbadge" :class="qualityBadge(shotEntry(sc, shot.idx).quality).cls">{{ qualityBadge(shotEntry(sc, shot.idx).quality).text }}</span>
                        <span v-if="candidateBadge(sc, shot)" class="qbadge q-info">{{ candidateBadge(sc, shot).text }}</span>
                        <span v-if="continuityBadge(sc, shot)" class="qbadge" :class="continuityBadge(sc, shot).cls" :title="continuityBadge(sc, shot).title">{{ continuityBadge(sc, shot).text }}</span>
                      </span>
                    </button>
                  </article>
                </div>

                <section v-if="selectedGeneration && Number(selectedGeneration.scene.scene_index) === Number(sc.scene_index)" class="generation-detail" aria-label="当前镜头详情">
                  <div class="generation-detail-head">
                    <div><span class="detail-kicker">当前镜头详情</span><strong>镜 {{ selectedGeneration.shot.idx + 1 }}</strong><small>场景 {{ Number(selectedGeneration.scene.scene_index) + 1 }} · {{ Number(selectedGeneration.shot.duration_sec || 5).toFixed(1) }}s</small></div>
                    <span class="project-state" :class="shotState(selectedGeneration.scene, selectedGeneration.shot).k === 'done' ? 'ps-done' : 'ps-pending'">{{ shotState(selectedGeneration.scene, selectedGeneration.shot).k === 'done' ? '已完成' : '生成中' }}</span>
                  </div>
                  <div class="generation-detail-grid">
                    <div class="generation-detail-main">
                      <div class="quality-signals">
                        <span v-if="qualityBadge(shotEntry(selectedGeneration.scene, selectedGeneration.shot.idx).quality)" class="qbadge" :class="qualityBadge(shotEntry(selectedGeneration.scene, selectedGeneration.shot.idx).quality).cls" :title="qualityBadge(shotEntry(selectedGeneration.scene, selectedGeneration.shot.idx).quality).title">{{ qualityBadge(shotEntry(selectedGeneration.scene, selectedGeneration.shot.idx).quality).text }}</span>
                        <span v-if="promptPreflightBadge(selectedGeneration.scene, selectedGeneration.shot)" class="qbadge" :class="promptPreflightBadge(selectedGeneration.scene, selectedGeneration.shot).cls" :title="promptPreflightBadge(selectedGeneration.scene, selectedGeneration.shot).title">{{ promptPreflightBadge(selectedGeneration.scene, selectedGeneration.shot).text }}</span>
                        <span v-if="candidateBadge(selectedGeneration.scene, selectedGeneration.shot)" class="qbadge q-info" :title="candidateBadge(selectedGeneration.scene, selectedGeneration.shot).title">{{ candidateBadge(selectedGeneration.scene, selectedGeneration.shot).text }}</span>
                        <span v-if="continuityBadge(selectedGeneration.scene, selectedGeneration.shot)" class="qbadge" :class="continuityBadge(selectedGeneration.scene, selectedGeneration.shot).cls" :title="continuityBadge(selectedGeneration.scene, selectedGeneration.shot).title">{{ continuityBadge(selectedGeneration.scene, selectedGeneration.shot).text }}</span>
                        <span v-if="durationAdjustment(selectedGeneration.scene, selectedGeneration.shot)" class="duration-adjust" :title="durationAdjustment(selectedGeneration.scene, selectedGeneration.shot).title">{{ durationAdjustment(selectedGeneration.scene, selectedGeneration.shot).text }}</span>
                      </div>

                      <div v-if="productionFact(selectedGeneration.scene, selectedGeneration.shot)" class="shot-production-facts">
                        <div><span>模型</span><strong>{{ routeText(productionFact(selectedGeneration.scene, selectedGeneration.shot)) }}</strong></div>
                        <div><span>请求</span><strong>{{ productionFact(selectedGeneration.scene, selectedGeneration.shot).request_attempts || 0 }} 次 · 重试 {{ productionFact(selectedGeneration.scene, selectedGeneration.shot).retry_count || 0 }}</strong></div>
                        <div><span>生成</span><strong>{{ formatSeconds(productionFact(selectedGeneration.scene, selectedGeneration.shot).generation_seconds) }}</strong></div>
                        <div v-if="hasActualCost(shotMetric(selectedGeneration.scene, selectedGeneration.shot).cost)"><span>实际费用</span><strong>{{ actualCostText(shotMetric(selectedGeneration.scene, selectedGeneration.shot).cost) }}</strong></div>
                      </div>

                      <div v-if="shotState(selectedGeneration.scene, selectedGeneration.shot).k === 'done'" class="generation-action-bar">
                        <div class="detail-tools-label">镜头操作</div>
                        <template v-if="!isPromptEditing(selectedGeneration.scene, selectedGeneration.shot)">
                          <div class="inspector-actions">
                            <button class="act" @click="regen(selectedGeneration.scene.scene_index, selectedGeneration.shot.idx)"><RefreshCw :size="14" />重新生成</button>
                            <button class="ghost" @click="openEdit(selectedGeneration.scene, selectedGeneration.shot)"><Pencil :size="14" />编辑提示词</button>
                          </div>
                          <details class="shot-rework-settings">
                            <summary><SlidersHorizontal :size="14" />返工设置</summary>
                            <div class="shot-rework-settings-body">
                              <label class="regen-reason"><span>原因</span><select v-model="regenReasons[regenReasonKey(selectedGeneration.scene.scene_index, selectedGeneration.shot.idx)]"><option v-for="option in regenReasonOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                              <div class="dimension-locks compact" aria-label="返工锁定约束"><span><Lock :size="13" />保持不变</span><label><input v-model="reworkLocks.identity" type="checkbox" />人物</label><label><input v-model="reworkLocks.composition" type="checkbox" />构图</label><label><input v-model="reworkLocks.motion" type="checkbox" />动作</label><label><input v-model="reworkLocks.audio" type="checkbox" />声音</label></div>
                            </div>
                          </details>
                        </template>
                        <div v-else class="prompt-edit-actions">
                          <span v-if="shotPromptDirty(selectedGeneration.scene, selectedGeneration.shot)" class="inline-dirty"><AlertTriangle :size="13" />有未保存的修改</span>
                          <span v-else class="muted">请直接修改下方内容</span>
                          <div class="inspector-actions">
                            <button class="act" :disabled="promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy || !shotPromptDirty(selectedGeneration.scene, selectedGeneration.shot)" @click="submitEdit(selectedGeneration.scene, selectedGeneration.shot)"><RefreshCw :size="14" />{{ promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy ? '重生成中…' : '保存并重生成' }}</button>
                            <button class="ghost" :disabled="promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy" @click="cancelEdit(selectedGeneration.scene, selectedGeneration.shot)">取消</button>
                          </div>
                          <div v-if="promptEditState(selectedGeneration.scene, selectedGeneration.shot).msg" class="muted">{{ promptEditState(selectedGeneration.scene, selectedGeneration.shot).msg }}</div>
                        </div>
                      </div>

                      <div class="generation-detail-copy">
                        <section>
                          <h3>导演稿</h3>
                          <textarea v-if="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot)" class="inline-prompt-editor director"
                            rows="6" v-model="promptEditState(selectedGeneration.scene, selectedGeneration.shot).dd"
                            :disabled="promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy"></textarea>
                          <p v-else>{{ reviewableVisualPrompt(selectedGeneration.shot.visual_desc, selectedGeneration.shot.director_desc) || '旧版分镜待转换为中文' }}</p>
                        </section>
                        <section v-if="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot) || selectedGeneration.shot.audio_desc">
                          <h3>台词与声音</h3>
                          <textarea v-if="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot)" class="inline-prompt-editor"
                            rows="4" v-model="promptEditState(selectedGeneration.scene, selectedGeneration.shot).ad"
                            :disabled="promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy"></textarea>
                          <div v-else class="audio-summary"><Volume2 :size="15" />{{ reviewableAudioPrompt(selectedGeneration.shot.audio_desc) }}</div>
                        </section>
                        <section v-if="continuityState(selectedGeneration.scene, selectedGeneration.shot)">
                          <h3>连续性账本</h3>
                          <p>{{ continuityBadge(selectedGeneration.scene, selectedGeneration.shot).title }}</p>
                          <ul v-if="continuityRepairs(selectedGeneration.scene, selectedGeneration.shot).length" class="continuity-repairs">
                            <li v-for="item in continuityRepairs(selectedGeneration.scene, selectedGeneration.shot)" :key="item.code">
                              <strong>{{ item.message }}</strong>
                              <span>{{ (item.actions || []).join('；') }}</span>
                            </li>
                          </ul>
                        </section>
                        <details v-if="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot) || reviewableVisualPrompt(selectedGeneration.shot.visual_desc, selectedGeneration.shot.director_desc)" class="shot-prompt"
                          :open="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot)">
                          <summary>模型执行稿</summary>
                          <textarea v-if="isPromptEditing(selectedGeneration.scene, selectedGeneration.shot)" class="inline-prompt-editor model-prompt"
                            rows="7" v-model="promptEditState(selectedGeneration.scene, selectedGeneration.shot).vd"
                            :disabled="promptEditState(selectedGeneration.scene, selectedGeneration.shot).busy"></textarea>
                          <span v-else>{{ reviewableVisualPrompt(selectedGeneration.shot.visual_desc, selectedGeneration.shot.director_desc) }}</span>
                        </details>
                      </div>
                    </div>

                    <div class="generation-detail-history">
                      <div v-if="shotState(selectedGeneration.scene, selectedGeneration.shot).k === 'done'" class="shot-history-wrap">
                        <button class="shot-history-toggle" type="button" :aria-expanded="historyState(selectedGeneration.scene, selectedGeneration.shot).open" @click="toggleHistory(selectedGeneration.scene, selectedGeneration.shot)">
                          <span class="shot-history-title"><History :size="15" /><span><strong>视频历史版本</strong><small>预览并选择当前镜头使用的视频</small></span></span>
                          <ChevronUp v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).open" :size="15" />
                          <ChevronDown v-else :size="15" />
                        </button>
                        <div v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).open" class="shot-history">
                          <div v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).loading" class="muted">加载中…</div>
                          <div v-else-if="historyState(selectedGeneration.scene, selectedGeneration.shot).error" class="muted">{{ historyState(selectedGeneration.scene, selectedGeneration.shot).error }}</div>
                          <template v-else-if="activeHistoryGroup(selectedGeneration.scene, selectedGeneration.shot)">
                            <div v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).compare" class="version-compare">
                              <div class="version-compare-head"><strong><Columns2 :size="14" />版本对比</strong><button type="button" title="退出对比" aria-label="退出版本对比" @click="clearVersionComparison(selectedGeneration.scene, selectedGeneration.shot)"><X :size="14" /></button></div>
                              <div class="version-compare-grid">
                                <figure><video controls preload="metadata" :src="shotState(selectedGeneration.scene, selectedGeneration.shot).video"></video><figcaption>当前使用</figcaption></figure>
                                <figure><video controls preload="metadata" :src="versionUrl(historyState(selectedGeneration.scene, selectedGeneration.shot).compare)"></video><figcaption>历史 v{{ historyState(selectedGeneration.scene, selectedGeneration.shot).compare.display_version || historyState(selectedGeneration.scene, selectedGeneration.shot).compare.version }}</figcaption></figure>
                              </div>
                            </div>
                            <div v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).annotationTarget" class="version-annotation-editor">
                              <div class="version-annotation-head">
                                <strong><MessageSquarePlus :size="14" />v{{ historyState(selectedGeneration.scene, selectedGeneration.shot).annotationTarget.display_version || historyState(selectedGeneration.scene, selectedGeneration.shot).annotationTarget.version }} 批注</strong>
                                <button type="button" title="关闭批注" aria-label="关闭版本批注" @click="closeVersionAnnotation(selectedGeneration.scene, selectedGeneration.shot)"><X :size="14" /></button>
                              </div>
                              <div v-if="historyState(selectedGeneration.scene, selectedGeneration.shot).annotationLoading" class="muted">加载批注…</div>
                              <div v-else-if="historyState(selectedGeneration.scene, selectedGeneration.shot).annotations.length" class="version-annotation-list">
                                <div v-for="annotation in historyState(selectedGeneration.scene, selectedGeneration.shot).annotations" :key="annotation.annotation_id" class="version-annotation-item">
                                  <span v-if="formatAnnotationTimecode(annotation.timecode_seconds)" class="version-annotation-timecode">{{ formatAnnotationTimecode(annotation.timecode_seconds) }}</span>
                                  <p>{{ annotation.text }}</p>
                                  <small>{{ annotation.author || '审核记录' }} · {{ fmtVersionTime(annotation.created_at) }}</small>
                                </div>
                              </div>
                              <div v-else-if="!historyState(selectedGeneration.scene, selectedGeneration.shot).annotationLoading" class="muted">这个版本还没有批注</div>
                              <div class="version-annotation-form">
                                <label><span>时间点（秒）</span><input v-model="historyState(selectedGeneration.scene, selectedGeneration.shot).annotationTime" type="number" min="0" step="0.1" placeholder="可选" /></label>
                                <label><span>批注意见</span><textarea v-model="historyState(selectedGeneration.scene, selectedGeneration.shot).annotationText" maxlength="1000" placeholder="记录这个版本需要关注的问题"></textarea></label>
                                <div class="version-annotation-submit"><span class="muted">{{ historyState(selectedGeneration.scene, selectedGeneration.shot).annotationError }}</span><button class="act" type="button" :disabled="historyState(selectedGeneration.scene, selectedGeneration.shot).annotationSaving || !historyState(selectedGeneration.scene, selectedGeneration.shot).annotationText.trim()" @click="saveVersionAnnotation(selectedGeneration.scene, selectedGeneration.shot)"><Save :size="13" />保存批注</button></div>
                              </div>
                            </div>
                            <div class="version-group">
                              <div class="version-kind"><span>视频版本</span><small>{{ activeHistoryGroup(selectedGeneration.scene, selectedGeneration.shot).versions.length }} 个版本</small></div>
                              <article v-for="item in activeHistoryGroup(selectedGeneration.scene, selectedGeneration.shot).versions" :key="item.artifact_id" class="version-card" :class="{ active: item.status === 'active' }">
                                <video class="version-preview version-video-preview" controls preload="metadata" :src="versionUrl(item)"></video>
                                <div class="version-card-body">
                                  <div class="version-card-head"><strong>v{{ item.display_version || item.version }}</strong><span class="version-status" :class="'vs-' + item.status">{{ versionStatusLabel(item.status) }}</span></div>
                                  <span class="version-time">{{ fmtVersionTime(item.created_at) }}</span>
                                  <div class="version-card-actions">
                                    <span v-if="item.status === 'active'" class="version-current"><Check :size="12" />当前使用</span>
                                    <button class="version-annotation-button" type="button" title="添加版本批注" :aria-label="'批注视频 v' + (item.display_version || item.version)" @click="openVersionAnnotation(selectedGeneration.scene, selectedGeneration.shot, item)"><MessageSquarePlus :size="12" />批注</button>
                                    <button v-if="item.status !== 'active'" class="version-compare-button" type="button" title="与当前版本并排对比" @click="compareVersion(selectedGeneration.scene, selectedGeneration.shot, item)"><Columns2 :size="12" />对比</button>
                                    <button v-if="item.status !== 'active'" class="version-select" type="button" :disabled="snap && snap.busy" @click="selectVersion(selectedGeneration.scene, selectedGeneration.shot, item)">选择此版本</button>
                                  </div>
                                </div>
                              </article>
                            </div>
                          </template>
                          <div v-else class="muted">暂无视频历史版本</div>
                        </div>
                      </div>
                    </div>
                  </div>
                </section>
              </section>
            </main>
          </div>
        </template>
      </div>

      <!-- 终审 / 完成：成片 -->
      <div v-else-if="view === 'final' || view === 'completed'" class="review-stage">
        <div class="stage-heading"><div><h2>成片制作</h2><span>{{ editPlanOutputDuration.toFixed(1) }} 秒 · {{ totals.total }} 个镜头</span></div></div>
        <div v-if="hasFinal" class="final-review-layout">
          <main>
            <video class="preview final-player" controls preload="metadata" :src="finalUrl"></video>
            <div class="final-timeline" aria-label="成片镜头时间线"><span v-for="clip in finalTimelineClips" :key="clip.key" :style="{ flexGrow: clip.duration }">{{ clip.label }} · {{ clip.duration.toFixed(1) }}s</span></div>
            <div class="final-tool-tabs" role="tablist" aria-label="成片交付工具">
              <button class="final-tool-tab" :class="{ active: editPlanOpen }" type="button" role="tab" :aria-selected="editPlanOpen" :aria-expanded="editPlanOpen" @click="toggleEditPlan"><Scissors :size="15" />剪辑方案<ChevronUp v-if="editPlanOpen" :size="14" /><ChevronDown v-else :size="14" /></button>
              <button class="final-tool-tab" :class="{ active: subtitleOpen }" type="button" role="tab" :aria-selected="subtitleOpen" :aria-expanded="subtitleOpen" @click="toggleSubtitleTimeline"><Captions :size="15" />字幕时间线<ChevronUp v-if="subtitleOpen" :size="14" /><ChevronDown v-else :size="14" /></button>
            </div>
            <section v-if="editPlanOpen" class="timeline-editor" role="tabpanel" aria-label="成片剪辑方案">
              <div v-if="editPlanLoading" class="muted">加载剪辑方案…</div>
              <template v-else-if="editPlan">
                <div v-if="editPlan.stale_saved_plan" class="timeline-warning"><AlertTriangle :size="14" />镜头源已更新，已载入新的默认方案</div>
                <div class="timeline-settings">
                  <label><span>镜头转场</span><select v-model="editPlan.transition.type" @change="changeEditTransition"><option value="none">硬切</option><option value="crossfade">交叉溶解</option><option value="fade">淡入淡出</option></select></label>
                  <label><span>转场时长</span><input v-model.number="editPlan.transition.duration" type="number" min="0.1" :max="editPlanTransitionMax" step="0.1" :disabled="editPlan.transition.type === 'none'" /><small>秒</small></label>
                  <div class="timeline-duration"><span>输出时长</span><strong>{{ editPlanOutputDuration.toFixed(1) }} 秒</strong></div>
                </div>
                <ol class="timeline-clip-list">
                  <li v-for="(clip, index) in editPlan.clips" :key="clip.clip_id" class="timeline-clip-row">
                    <span class="timeline-order">{{ index + 1 }}</span>
                    <div class="timeline-clip-name"><strong>{{ clip.label }}</strong><small>原片 {{ Number(clip.source_duration).toFixed(1) }} 秒</small></div>
                    <label><span>入点</span><input v-model.number="clip.trim_start" type="number" min="0" :max="Math.max(0, Number(clip.trim_end) - 0.1)" step="0.1" /><small>秒</small></label>
                    <label><span>出点</span><input v-model.number="clip.trim_end" type="number" :min="Number(clip.trim_start) + 0.1" :max="clip.source_duration" step="0.1" /><small>秒</small></label>
                    <span class="timeline-kept">保留 {{ Math.max(0, Number(clip.trim_end) - Number(clip.trim_start)).toFixed(1) }} 秒</span>
                    <div class="timeline-reorder">
                      <button type="button" title="上移镜头" :aria-label="'上移' + clip.label" :disabled="index === 0" @click="moveEditClip(index, -1)"><ArrowUp :size="14" /></button>
                      <button type="button" title="下移镜头" :aria-label="'下移' + clip.label" :disabled="index === editPlan.clips.length - 1" @click="moveEditClip(index, 1)"><ArrowDown :size="14" /></button>
                    </div>
                  </li>
                </ol>
                <div class="timeline-editor-actions">
                  <span :class="editPlanError ? 'timeline-error' : 'muted'">{{ editPlanError || editPlanMsg }}</span>
                  <button class="ghost" type="button" :disabled="editPlanBusy || editPlan.source_status !== 'ready' || (snap && snap.busy)" @click="resetEditPlan"><RotateCcw :size="14" />恢复原始成片</button>
                  <button class="ghost" type="button" :disabled="editPlanBusy || !editPlanDirty || (snap && snap.busy)" @click="saveEditPlan"><Save :size="14" />保存方案</button>
                  <button class="act" type="button" :disabled="editPlanBusy || (snap && snap.busy)" @click="renderEditPlan"><RefreshCw :size="14" />重新合成</button>
                </div>
              </template>
              <div v-else class="timeline-error">{{ editPlanError }}</div>
            </section>
            <section v-if="subtitleOpen" class="timeline-editor subtitle-editor" role="tabpanel" aria-label="成片字幕时间线">
              <div v-if="subtitleLoading" class="subtitle-loading muted">加载字幕时间线…</div>
              <template v-else-if="subtitleTimeline">
                <div v-if="subtitleTimeline.stale_saved_timeline" class="timeline-warning"><AlertTriangle :size="14" />成片或配音已更新，已载入生成字幕</div>
                <template v-if="subtitleTimeline.lines && subtitleTimeline.lines.length">
                  <div class="subtitle-overview" aria-label="字幕时间分布">
                    <span v-for="line in subtitleTimeline.lines" :key="line.line_id" :style="subtitlePosition(line)" :title="line.text">{{ Number(line.order) + 1 }}</span>
                  </div>
                  <ol class="subtitle-line-list">
                    <li v-for="line in subtitleTimeline.lines" :key="line.line_id" class="subtitle-line-row">
                      <span class="timeline-order">{{ Number(line.order) + 1 }}</span>
                      <div class="subtitle-line-source"><strong>{{ line.speaker || '旁白' }}</strong><small>{{ line.shot_idx == null ? `场景 ${Number(line.scene_index) + 1}` : `场景 ${Number(line.scene_index) + 1} · 镜头 ${Number(line.shot_idx) + 1}` }}</small></div>
                      <label class="subtitle-text-field"><span>字幕</span><textarea v-model="line.text" maxlength="500" rows="2"></textarea></label>
                      <label><span>开始</span><input v-model.number="line.start" type="number" min="0" :max="Math.max(0, Number(line.end) - 0.1)" step="0.1" /><small>秒</small></label>
                      <label><span>结束</span><input v-model.number="line.end" type="number" :min="Number(line.start) + 0.1" :max="subtitleTimeline.duration" step="0.1" /><small>秒</small></label>
                      <span class="subtitle-line-duration">{{ Math.max(0, Number(line.end) - Number(line.start)).toFixed(1) }} 秒</span>
                    </li>
                  </ol>
                </template>
                <div v-else class="subtitle-empty muted">当前成片没有对白字幕</div>
                <div class="timeline-editor-actions">
                  <span :class="subtitleError ? 'timeline-error' : 'muted'">{{ subtitleError || subtitleMsg }}</span>
                  <button class="ghost" type="button" :disabled="subtitleBusy || !subtitleTimeline.lines.length || (snap && snap.busy)" @click="resetSubtitleTimeline"><RotateCcw :size="14" />恢复生成字幕</button>
                  <a v-if="subtitleTimeline.lines.length" class="ghost subtitle-download" :href="subtitleDownloadUrl" download="成片字幕.srt"><Download :size="14" />下载 SRT</a>
                  <button class="act" type="button" :disabled="subtitleBusy || !subtitleDirty || !subtitleTimeline.lines.length || (snap && snap.busy)" @click="saveSubtitleTimeline"><Save :size="14" />保存字幕文件</button>
                </div>
              </template>
              <div v-else class="timeline-error subtitle-loading">{{ subtitleError }}</div>
            </section>
          </main>
          <aside class="final-inspector">
            <section><h3>发布检查</h3>
              <div class="publish-check"><CheckCircle2 :size="16" /><div><strong>{{ totals.videos }}/{{ totals.total }} 镜头完成</strong><span>分镜视频已合成</span></div></div>
              <div class="publish-check"><component :is="allQualityPass ? CheckCircle2 : AlertTriangle" :size="16" /><div><strong>{{ allQualityPass ? '质量检查通过' : qualityIssueCount + ' 个镜头需要复核' }}</strong><span>以镜头审核结果为准</span></div></div>
              <div class="publish-check"><CheckCircle2 :size="16" /><div><strong>成片文件已就绪</strong><span>{{ costLabel ? '预计成本 ' + costLabel : '可下载导出' }}</span></div></div>
            </section>
            <section><h3>导出与发布</h3>
              <a class="act final-action" :href="finalUrl" download="成片.mp4"><Download :size="15" />下载成片</a>
              <button v-if="canPublish && shareEnabled" class="act final-action" @click="emit('publish')"><Share2 :size="15" />生成分享链接</button>
              <button class="ghost final-action" @click="emit('cost')"><Calculator :size="15" />成本明细</button>
              <button v-if="canClean" class="ghost final-action danger-text" @click="emit('clean')"><Trash2 :size="15" />清理中间文件</button>
            </section>
          </aside>
        </div>
        <div v-else class="muted">成片尚未生成；完成镜头审核后将在此合成。</div>
      </div>

      <div v-else class="muted">选择上方流程查看对应内容。</div>
    </template>
  </div>
</template>
