<script setup>
import { ref, watch, onUnmounted } from 'vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close', 'open-project'])
const jobs = ref([])
const loading = ref(false)
const error = ref('')
let timer = null

const activeStates = new Set(['queued', 'running', 'waiting_provider', 'retry_wait', 'cancel_requested'])
const typeLabel = (value) => ({
  'workflow.start_topic': '创建项目',
  'workflow.approve': '推进生成',
  'workflow.revise': '按意见修改',
  'workflow.resume': '恢复生成',
  'workflow.regenerate_shot': '重生成镜头',
  'workflow.regenerate_shots': '批量返工镜头',
  'workflow.preview_keyframes': '关键帧预览',
}[value] || '生成任务')
const stateLabel = (value) => ({
  queued: '排队中', running: '生成中', waiting_provider: '等待云端', retry_wait: '等待重试',
  cancel_requested: '取消中', succeeded: '已完成', failed: '失败', interrupted: '已中断', canceled: '已取消',
}[value] || value)
const stateClass = (value) => activeStates.has(value) ? 'active' : (value === 'succeeded' ? 'done' : 'failed')
function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  try {
    const result = await api('GET', '/api/production/jobs')
    jobs.value = result.jobs || []
    error.value = ''
  } catch (e) { error.value = '任务加载失败：' + e.message }
  loading.value = false
}
async function cancel(job) {
  if (!await confirmModal('取消这个生成任务？已完成的中间产物会保留。', { okText: '取消任务', danger: true })) return
  try { await api('POST', '/api/production/jobs/' + job.job_id + '/cancel'); await load() }
  catch (e) { error.value = '取消失败：' + ((e.body && e.body.error) || e.message) }
}
function openProject(job) {
  if (job.project_id) emit('open-project', job.project_id)
}
function stopPolling() { if (timer) { clearInterval(timer); timer = null } }
watch(() => props.open, (open) => {
  stopPolling()
  if (open) { load(); timer = setInterval(load, 3000) }
}, { immediate: true })
onUnmounted(stopPolling)
</script>

<template>
  <div v-if="open" class="task-mask" @click.self="emit('close')">
    <section class="task-center" role="dialog" aria-modal="true" aria-label="任务中心">
      <header>
        <div><strong>任务中心</strong><span>生成任务会在关闭页面后继续运行</span></div>
        <button class="task-close" title="关闭" aria-label="关闭任务中心" @click="emit('close')">×</button>
      </header>
      <div class="task-list">
        <div v-if="loading && !jobs.length" class="muted">加载中…</div>
        <div v-else-if="error" class="errnote">{{ error }}</div>
        <div v-else-if="!jobs.length" class="task-empty">暂无生成任务</div>
        <article v-for="job in jobs" :key="job.job_id" class="task-item">
          <div class="task-main">
            <div class="task-title">{{ typeLabel(job.job_type) }}</div>
            <span class="task-state" :class="stateClass(job.internal_state)">{{ stateLabel(job.internal_state) }}</span>
          </div>
          <div v-if="job.last" class="task-last">{{ job.last }}</div>
          <div v-if="job.error" class="task-error">{{ job.error }}</div>
          <div class="task-meta">{{ formatTime(job.updated_at || job.created_at) }}<span v-if="job.steps"> · {{ job.steps }} 条进度</span></div>
          <div class="task-actions">
            <button v-if="job.project_id" class="ghost" @click="openProject(job)">{{ stateClass(job.internal_state) === 'failed' ? '打开处理' : '打开项目' }}</button>
            <button v-if="activeStates.has(job.internal_state)" class="ghost danger-text" @click="cancel(job)">取消</button>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
