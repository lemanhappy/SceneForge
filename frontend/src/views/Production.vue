<script setup>
import { computed, ref, onMounted, watch } from 'vue'
import { ArrowRight, CheckCircle2, Clock3, FolderOpen, ListTodo, Search, Trash2 } from '@lucide/vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { stageInfo, stageLabel } from '../lib/stages.js'
import CreateForm from '../components/CreateForm.vue'
import SessionDetail from '../components/SessionDetail.vue'
import TaskCenter from '../components/TaskCenter.vue'

const props = defineProps({
  historyMode: { type: Boolean, default: false },
  selectedSid: { type: String, default: null },
  resetKey: { type: Number, default: 0 },
})
const emit = defineEmits(['sessions-changed', 'history-selection'])

const sessions = ref([])
const activeSid = ref(null)
const listMsg = ref('加载中…')
const sessionQuery = ref('')
const historyFilter = ref('all')
const taskCenterOpen = ref(false)
const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
})
const filterOptions = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '进行中' },
  { key: 'complete', label: '已完成' },
]

const completedCount = computed(() => sessions.value.filter((item) => isComplete(item)).length)
const activeCount = computed(() => sessions.value.length - completedCount.value)
const filteredSessions = computed(() => {
  const query = sessionQuery.value.trim().toLocaleLowerCase()
  return sessions.value.filter((item) => {
    if (historyFilter.value === 'complete' && !isComplete(item)) return false
    if (historyFilter.value === 'active' && isComplete(item)) return false
    return !query || `${item.idea || ''} ${item.session_id || ''} ${item.series_title || ''} ${item.episode_title || ''}`.toLocaleLowerCase().includes(query)
  })
})

async function loadSessions() {
  try {
    const data = await api('GET', '/api/production')
    sessions.value = data.sessions || []
    listMsg.value = sessions.value.length ? '' : '暂无历史创作'
  } catch (e) {
    listMsg.value = '加载失败：' + e.message
  }
}
onMounted(loadSessions)
watch(() => props.selectedSid, (sid) => {
  if (props.historyMode) activeSid.value = sid || null
}, { immediate: true })
watch(() => props.resetKey, () => {
  if (!props.historyMode) activeSid.value = null
})

function isComplete(session) {
  return stageInfo(session).phase === 'done'
}
function stagePercent(session) {
  const info = stageInfo(session)
  if (info.phase === 'done') return 100
  const phaseProgress = ['review', 'error', 'interrupted'].includes(info.phase) ? 0.65 : (info.phase === 'generating' || info.phase === 'revising' ? 0.35 : 0.12)
  return Math.round(((info.idx + phaseProgress) / 4) * 100)
}
function formatUpdated(value) {
  if (!value) return '时间未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : dateFormatter.format(date).replaceAll('/', '-')
}
function select(sid) {
  activeSid.value = sid
  if (props.historyMode) emit('history-selection', sid)
}
function projectTitle(session) {
  if (session.series_id) {
    return `${session.series_title || '连续短剧'} · 第 ${session.episode_number || '?'} 集${session.episode_title ? '：' + session.episode_title : ''}`
  }
  return session.idea || ('未命名创作 ' + session.session_id)
}
function onCreated(sid) {
  activeSid.value = sid
  loadSessions()
  emit('sessions-changed')
}
function onSessionChanged() {
  loadSessions()
  emit('sessions-changed')
}
function openTaskProject(sid) {
  taskCenterOpen.value = false
  select(sid)
}

async function delSession(session) {
  if (!await confirmModal('删除创作「' + (session.idea || session.session_id) + '」？将一并删除其所有中间文件与成片，不可恢复。', { okText: '删除', danger: true })) return
  try {
    await api('DELETE', '/api/production/' + session.session_id)
    if (activeSid.value === session.session_id) activeSid.value = null
    await loadSessions()
    emit('sessions-changed')
  } catch (e) {
    await confirmModal('删除失败：' + ((e.body && e.body.error) || e.message), { okText: '知道了' })
  }
}

async function cleanupAll() {
  try {
    const preview = await api('GET', '/api/production/cleanup-all')
    if (!await confirmModal('清理 ' + preview.sessions_with_final + ' 个已完成创作的中间文件，约释放 ' + preview.freeable_mb + ' MB？成片和海报会保留，清理后不可续跑或重生成单镜。', { okText: '清理', danger: true })) return
    const result = await api('POST', '/api/production/cleanup-all')
    await confirmModal('已清理 ' + result.cleaned_sessions + ' 个创作，释放 ' + result.freed_mb + ' MB', { okText: '好' })
    loadSessions()
    emit('sessions-changed')
  } catch (e) {
    await confirmModal('清理失败：' + e.message, { okText: '知道了' })
  }
}
</script>

<template>
  <div class="cre">
    <div class="cre-work">
      <section v-if="historyMode && !activeSid" class="history-page" aria-labelledby="history-title">
        <header class="history-page-head">
          <div>
            <h1 id="history-title">历史创作</h1>
            <p>查看进度并继续未完成的短剧项目</p>
          </div>
          <div class="history-summary" aria-label="创作统计">
            <span><strong>{{ activeCount }}</strong> 进行中</span>
            <span><strong>{{ completedCount }}</strong> 已完成</span>
          </div>
        </header>

        <div class="history-toolbar">
          <label class="history-search">
            <Search :size="16" />
            <input v-model="sessionQuery" type="search" placeholder="搜索标题或项目编号" aria-label="搜索历史创作" />
          </label>
          <div class="history-filters" role="group" aria-label="筛选历史创作">
            <button v-for="option in filterOptions" :key="option.key" type="button"
              :class="{ active: historyFilter === option.key }" @click="historyFilter = option.key">
              {{ option.label }}
            </button>
          </div>
          <div class="history-tools">
            <button class="ghost" type="button" @click="taskCenterOpen = true"><ListTodo :size="16" />任务中心</button>
            <button class="ghost" type="button" @click="cleanupAll"><FolderOpen :size="16" />清理文件</button>
          </div>
        </div>

        <div class="history-list" aria-live="polite">
          <div class="history-list-head" aria-hidden="true">
            <span>创作项目</span><span>进度</span><span>最近更新</span><span>操作</span>
          </div>
          <div v-if="listMsg" class="history-empty">{{ listMsg }}</div>
          <div v-else-if="!filteredSessions.length" class="history-empty">没有符合条件的创作</div>
          <article v-for="session in filteredSessions" :key="session.session_id" class="history-row">
            <button class="history-project" type="button" @click="select(session.session_id)">
              <span class="history-project-icon" :class="{ complete: isComplete(session) }">
                <CheckCircle2 v-if="isComplete(session)" :size="18" />
                <Clock3 v-else :size="18" />
              </span>
              <span class="history-project-copy">
                <strong>{{ projectTitle(session) }}</strong>
                <small>{{ session.session_id }}</small>
              </span>
            </button>
            <div class="history-progress">
              <div><span>{{ stageLabel(session) }}</span><b>{{ stagePercent(session) }}%</b></div>
              <i><span :style="{ width: stagePercent(session) + '%' }"></span></i>
            </div>
            <time>{{ formatUpdated(session.updated_at) }}</time>
            <div class="history-actions">
              <button class="history-continue" type="button" @click="select(session.session_id)">
                {{ isComplete(session) ? '查看成片' : '继续创作' }}<ArrowRight :size="15" />
              </button>
              <button class="iconbtn danger" type="button" title="删除创作" :aria-label="'删除创作：' + (session.idea || session.session_id)" @click="delSession(session)">
                <Trash2 :size="16" />
              </button>
            </div>
          </article>
        </div>
      </section>

      <CreateForm v-else-if="!activeSid" @created="onCreated" @sessions-changed="onSessionChanged" />
      <SessionDetail v-else :key="activeSid" :sid="activeSid" @sessions-changed="onSessionChanged" />
    </div>
    <TaskCenter :open="taskCenterOpen" @close="taskCenterOpen = false" @open-project="openTaskProject" />
  </div>
</template>
