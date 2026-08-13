<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import {
  ArrowRight, Calculator, Check, Clapperboard, FileText, Film,
  LayoutPanelTop, Play, Square, Undo2,
} from '@lucide/vue'
import { api, watchJob } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { stageInfo, detailState, detailButtons, STEPS, PHASE_CN, STATE_HINT } from '../lib/stages.js'
import JobProgress from './JobProgress.vue'
import ReviewContent from './ReviewContent.vue'

const props = defineProps({
  sid: String,
})
const emit = defineEmits(['sessions-changed'])

const s = ref(null)
const msg = ref('')
const progress = ref([])
const stopping = ref(false)
const reviewKey = ref(0) // bump to force ReviewContent reload after save/regen/stage change
const stats = ref({ shots: 0, frames: 0, videos: 0, scenes: 0 }) // Hero 指标（来自 ReviewContent）
const costStat = ref('') // 预计成本
let gen = 0
let alive = true
onUnmounted(() => { alive = false })

const state = computed(() => detailState(s.value || {}))
const btn = computed(() => detailButtons(state.value, s.value || {}))
const info = computed(() => stageInfo(s.value || {}))
const activeView = ref(null) // which stage's content is shown in the main area
const STEP_ICONS = { script: FileText, storyboard: LayoutPanelTop, shot_video: Clapperboard, final: Film }
const gateIdx = (g) => { const i = STEPS.findIndex((x) => x.gate === g); return i < 0 ? 0 : i }
const steps = computed(() => STEPS.map((st, i) => {
  const done = i < info.value.idx || (i === info.value.idx && info.value.phase === 'done')
  return {
    label: st.label, gate: st.gate, icon: STEP_ICONS[st.gate], mark: done ? '✓' : (i + 1),
    done, cur: i === info.value.idx, viewing: st.gate === activeView.value,
    clickable: i <= info.value.idx, // 只能查看「已到达」的阶段
  }
}))
function setView(st) { if (st.clickable) activeView.value = st.gate }
const pending = computed(() => s.value && s.value.pending_review)
const showStatus = computed(() => pending.value && state.value !== 'busy' && state.value !== 'interrupted')
function normalizeStageActionCopy(value) {
  const action = btn.value.ok || '进入下一阶段'
  return String(value || '')
    .replaceAll('重发『通过』', '点击「' + action + '」')
    .replaceAll('点击“通过”', '点击「' + action + '」')
}
const lastError = computed(() => {
  const le = s.value && s.value.last_error
  if (!le || !(le.note || le.error)) return null
  const note = normalizeStageActionCopy(le.note || le.error)
  // 技术原因：error 是真实异常时才另起一行展示；budget_exceeded/moderation 等纯状态码已被 note 解释，不重复
  const codes = new Set(['budget_exceeded', 'moderation', 'exception', ''])
  const raw = (le.error && !codes.has(le.error) && le.error !== note) ? le.error : ''
  return { note, raw }
})
const budget = computed(() => {
  const bp = s.value && s.value.budget_preview
  if (!bp) return null
  const lim = bp.max_total_shots ? ('，上限 ' + bp.max_total_shots + ' 镜') : '（未设上限）'
  const per = (bp.per_scene && bp.per_scene.length) ? '（' + bp.per_scene.map((n, i) => '场景' + (i + 1) + ': ' + n + '镜').join('、') + '）' : ''
  return { ok: bp.ok, head: '本片共 ' + bp.shots + ' 个镜头' + (bp.scenes ? ('、' + bp.scenes + ' 个场景') : '') + lim + '。' + per }
})
const routeProblem = computed(() => {
  const route = s.value && s.value.provider_route_preview
  return route && route.ok === false ? route.note : ''
})
const projectMeta = computed(() => {
  const parts = []
  if (stats.value.shots) parts.push(stats.value.shots + ' 个镜头')
  if (stats.value.scenes) parts.push(stats.value.scenes + ' 个场景')
  if (costStat.value) parts.push('预计 ' + costStat.value)
  return parts.join(' · ')
})

async function load() {
  const myGen = ++gen
  msg.value = ''
  try { s.value = await api('GET', '/api/production/' + props.sid) }
  catch (e) { if (alive && gen === myGen) msg.value = '加载失败：' + e.message; return }
  if (!alive || gen !== myGen) return
  // default the viewed stage to the current pipeline stage; keep the user's manual
  // choice if it's still valid (≤ current), else snap to current.
  if (activeView.value == null || gateIdx(activeView.value) > info.value.idx) activeView.value = info.value.gate
  reviewKey.value++
  // 预计成本（独立拉取，不阻塞；成本估算未启用时留空）
  api('GET', '/api/production/' + props.sid + '/cost')
    .then((c) => {
      if (!alive || gen !== myGen) return
      costStat.value = c && c.estimated_min != null
        ? ((c.currency || '¥') + c.estimated_min + '–' + c.estimated_max)
        : ((c && c.estimated_total != null) ? ((c.currency || '¥') + c.estimated_total) : '')
    })
    .catch(() => {})
  if (state.value === 'busy' && s.value.job_id) {
    progress.value = []
    watchJob(s.value.job_id, (prog) => { if (alive && gen === myGen) progress.value = prog })
      .then(async (job) => {
        if (!alive || gen !== myGen) return
        await load()
        if (!alive) return
        stopping.value = false
        if (job && job.internal_state === 'canceled') msg.value = '本阶段已终止，已完成内容已保留。'
        else if (job && job.state === 'failed') msg.value = '失败：' + job.error
        else if (job && job.result && job.result.ok === false) msg.value = '⚠ 未继续：' + (job.result.note || job.result.error || '未通过，请查看上方说明')
      })
  } else if (state.value === 'busy') {
    setTimeout(() => { if (alive && gen === myGen) load() }, 3500)
  }
}
onMounted(load)
watch(() => props.sid, load)

async function act(fn, working) {
  msg.value = working || '已提交…'
  try { const r = await fn(); if (r && r.accepted === false) { msg.value = '上一步还在处理中，请稍候。'; return } await load(); emit('sessions-changed') }
  catch (e) { msg.value = '失败：' + ((e.body && e.body.error) || e.message) }
}
async function approve() {
  if (pending.value && pending.value.stage === 'storyboard') {
    const route = s.value && s.value.provider_route_preview
    if (route && route.ok === false) {
      msg.value = route.note || '当前模型不支持这些镜头，请调整后重试。'
      return
    }
    try {
      const c = await api('GET', '/api/production/' + props.sid + '/cost')
      const tierLabel = ({ economy: '省钱', balanced: '均衡', quality: '高质量' })[s.value.quality_tier] || '均衡'
      const amount = c.estimated_min != null
        ? ((c.currency || '¥') + c.estimated_min + ' - ' + (c.currency || '¥') + c.estimated_max)
        : ((c.currency || '¥') + (c.estimated_total ?? '待计算'))
      const preview = stats.value.shots
        ? ('\n关键帧预览：' + stats.value.frames + '/' + stats.value.shots)
        : ''
      const text = '将生成 ' + (c.shots || stats.value.shots || 0) + ' 个镜头的视频。\n质量档位：' + tierLabel + preview + '\n预计费用：' + amount
      if (!await confirmModal(text, { okText: '确认生成' })) return
    } catch (e) {
      msg.value = '费用确认失败：' + e.message
      return
    }
  }
  return act(() => api('POST', '/api/production/' + props.sid + '/approve'))
}
const resume = () => act(() => api('POST', '/api/production/' + props.sid + '/resume'), '继续生成中…')
const continueCancelled = () => act(
  () => api('POST', '/api/production/' + props.sid + '/continue-cancelled'),
  '继续本阶段中…',
)
async function stopStage() {
  const jobId = s.value && s.value.job_id
  if (!jobId || stopping.value) return
  if (!await confirmModal('终止当前阶段生成？已完成的中间结果会保留，之后可以继续生成。若请求已经提交到云端，仍可能产生费用。', { okText: '终止本阶段', danger: true })) return
  stopping.value = true
  msg.value = '正在终止当前阶段…'
  try {
    const result = await api('POST', '/api/production/jobs/' + jobId + '/cancel')
    if (result && result.ok === false) {
      stopping.value = false
      msg.value = '终止失败：' + (result.error || '当前任务不支持终止')
      return
    }
    await load()
    if (!(s.value && s.value.busy)) {
      stopping.value = false
      msg.value = '本阶段已终止，已完成内容已保留。'
    }
  } catch (e) {
    stopping.value = false
    msg.value = '终止失败：' + ((e.body && e.body.error) || e.message)
  }
}

// reopen
const reopenOpen = ref(false)
async function doReopen(gate) {
  const label = gate === 'script' ? '剧本' : '分镜脚本'
  if (!await confirmModal('退回到「' + label + '」？现有的分镜视频/成片' + (gate === 'script' ? '与分镜脚本' : '') + '将作废，需要重新生成。', { okText: '退回', danger: true })) return
  reopenOpen.value = false
  msg.value = '退回中…'
  try { const r = await api('POST', '/api/production/' + props.sid + '/reopen', { gate }); if (r && r.ok === false) { msg.value = '失败：' + (r.error || r.note || ''); return } await load(); emit('sessions-changed') }
  catch (e) { msg.value = '失败：' + ((e.body && e.body.error) || e.message) }
}

async function publish() {
  msg.value = '生成分享链接中…'
  try {
    const r = await api('POST', '/api/production/' + props.sid + '/publish')
    msg.value = r.url ? ('分享链接：' + r.url) : '未配置公开托管，请直接下载成片'
  }
  catch (e) { msg.value = '失败：' + e.message }
}
async function cost() {
  msg.value = '估算中…'
  try {
    const c = await api('GET', '/api/production/' + props.sid + '/cost')
    if (c.estimated_total == null) { msg.value = c.note || '未启用'; return }
    if (c.kind === 'plan' || c.estimated_min != null) {
      msg.value = '预计 ' + (c.currency || '¥') + c.estimated_min + ' - ' + (c.currency || '¥') + c.estimated_max
        + '（' + (c.scenes || 0) + ' 个场景 / ' + (c.shots || 0) + ' 个镜头）'
      return
    }
    const n = c.counts || {}
    msg.value = '估算 ' + (c.currency || '¥') + c.estimated_total + '（视频' + (n.video_clips || 0) + '镜 / 帧' + (n.images || 0) + ' / 配音' + (n.tts_lines || 0) + '句，仅估算）'
  } catch (e) { msg.value = '失败：' + e.message }
}
async function clean() {
  try {
    const p = await api('GET', '/api/production/' + props.sid + '/cleanup')
    if (!p.has_final) { msg.value = '无成片，暂不清理'; return }
    if (!await confirmModal('将删除分镜中间文件，约可释放 ' + p.freeable_mb + ' MB。删除后无法续跑/单镜重生成，成片与海报保留。继续？', { okText: '清理', danger: true })) return
    msg.value = '清理中…'
    const r = await api('POST', '/api/production/' + props.sid + '/cleanup')
    msg.value = r.ok ? ('已释放 ' + r.freed_mb + ' MB（删除 ' + r.removed + ' 项）') : ('失败：' + (r.error || ''))
  } catch (e) { msg.value = '失败：' + e.message }
}

function onReviewRefresh() { load(); emit('sessions-changed') }
</script>

<template>
  <div v-if="!s" class="cre-scroll"><div class="muted">{{ msg || '加载中…' }}</div></div>
  <template v-else>
    <!-- 项目工具条 + 四阶段工作流 -->
    <div class="cre-topbar">
      <div class="cre-projectbar">
        <div class="cre-project-copy">
          <div class="cre-project-title">{{ s.idea || ('项目 ' + sid) }}</div>
          <div class="cre-project-meta">{{ projectMeta || '准备中' }}</div>
        </div>
      </div>
      <div class="cre-workflowbar">
        <nav class="cre-steps" aria-label="创作流程">
          <button v-for="st in steps" :key="st.gate" type="button" class="seg"
            :class="{ done: st.done, on: st.viewing, disabled: !st.clickable }"
            :disabled="!st.clickable" :aria-current="st.viewing ? 'step' : undefined"
            :title="st.clickable ? st.label : st.label + '尚未生成'" @click="setView(st)">
            <component :is="st.icon" :size="16" />
            <span>{{ st.label }}</span>
            <Check v-if="st.done" :size="13" class="step-check" />
          </button>
        </nav>
        <div class="cre-workflow-actions">
          <span class="project-state" :class="'ps-' + info.phase">{{ PHASE_CN[info.phase] || info.phase }}</span>
          <div class="cre-acts">
            <button v-if="state === 'busy' && s.job_id" class="btn stop-stage" :disabled="stopping" @click="stopStage"><Square :size="14" />{{ stopping ? '终止中…' : '终止本阶段' }}</button>
            <button v-if="s.cancelled && state !== 'busy'" class="btn primary" @click="continueCancelled"><Play :size="15" />继续本阶段</button>
            <button v-else-if="btn.cont" class="btn primary" @click="resume"><Play :size="15" />继续生成</button>
            <button v-if="btn.ok && !s.cancelled" class="btn primary" @click="approve">{{ btn.ok }}<ArrowRight :size="15" /></button>
            <button v-if="btn.reopen" class="btn ghost" @click="reopenOpen = !reopenOpen" title="退回到剧本或分镜脚本修改"><Undo2 :size="15" />退回</button>
          </div>
        </div>
      </div>
    </div>

    <div class="cre-scroll">
      <div v-if="!showStatus && state !== 'busy' && state !== 'done'" class="muted" style="margin-top:8px">{{ STATE_HINT[state] || '' }}</div>
      <div v-if="s.cancelled" class="infonote">本阶段已终止，已完成的中间结果已保留；点击右上角「继续本阶段」可从当前进度继续。</div>
      <div v-if="lastError" class="errnote">
        ⚠ 上次未通过：{{ lastError.note }}
        <div v-if="lastError.raw" style="margin-top:5px;font-weight:400;opacity:.9;word-break:break-all">技术原因：{{ lastError.raw }}</div>
      </div>
      <div v-if="budget" :class="budget.ok ? 'infonote' : 'errnote'">
        {{ budget.ok ? '📐 ' : '⚠ ' }}{{ budget.head }}{{ budget.ok ? '' : ' 进入镜头生成前请先精简（可在下方分镜脚本里删减镜头）。' }}
      </div>
      <div v-if="routeProblem" class="errnote">⚠ {{ routeProblem }}</div>

      <!-- live progress while busy -->
      <JobProgress v-if="state === 'busy'" :progress="progress" compact />

      <!-- 次级操作 -->
      <div class="stage-tools">
        <button v-if="btn.cost && activeView !== 'final' && activeView !== 'shot_video'" class="ghost" @click="cost"><Calculator :size="15" />成本估算</button>
        <span class="muted">{{ msg }}</span>
      </div>

      <!-- reopen box -->
      <div v-if="reopenOpen" class="revbox">
        <label style="font-weight:600">退回到哪一步重做？</label>
        <div class="muted" style="margin:2px 0 8px">退回后下游会作废、需重新生成（费时费钱）。修改完成后，点击右上角的阶段按钮继续。</div>
        <div class="row">
          <button class="ghost" @click="doReopen('storyboard')">退回·分镜脚本</button>
          <button class="ghost" @click="doReopen('script')">退回·剧本</button>
          <button class="ghost" @click="reopenOpen = false">取消</button>
        </div>
      </div>

      <div class="stage-content">
        <ReviewContent :key="reviewKey" :sid="sid" :snap="s" :view="activeView" :can-revise="btn.rev"
          :can-publish="btn.pub" :can-clean="btn.clean" :can-cost="btn.cost" :cost-label="costStat"
          @refresh="onReviewRefresh" @stats="stats = $event" @publish="publish" @clean="clean" @cost="cost" @reopen="doReopen" />
      </div>
    </div>
  </template>
</template>
