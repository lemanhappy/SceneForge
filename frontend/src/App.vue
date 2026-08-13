<script setup>
import { ref, computed, onMounted } from 'vue'
import { Boxes, ChevronRight, Clapperboard, History, Palette, Search, Settings as SettingsIcon, WandSparkles } from '@lucide/vue'
import Production from './views/Production.vue'
import Creation from './views/Creation.vue'
import AssetModels from './views/AssetModels.vue'
import Skills from './views/Skills.vue'
import AutoPost from './views/AutoPost.vue'
import Settings from './views/Settings.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import Lightbox from './components/Lightbox.vue'
import { api } from './lib/api.js'
import { stageLabel } from './lib/stages.js'
import { applyTheme } from './lib/theme.js'

const TABS = [
  { key: 'creation', icon: Clapperboard, label: '新建创作', comp: Creation },
  { key: 'history', icon: History, label: '历史创作', comp: Production, props: { historyMode: true } },
  { key: 'assets', icon: Boxes, label: '资产模型', comp: AssetModels },
  { key: 'skills', icon: Palette, label: 'Skill 市场', comp: Skills },
  { key: 'edit', icon: WandSparkles, label: '自动后期', comp: AutoPost },
]
const BOTTOM = [{ key: 'config', icon: SettingsIcon, label: '设置', comp: Settings }]
const ALL = [...TABS, ...BOTTOM]
const active = ref('creation')
const selectedHistorySid = ref(null)
const createRequestKey = ref(0)
const sidebarSessions = ref([])
const sidebarQuery = ref('')
const sidebarListMsg = ref('加载中…')
const current = computed(() => ALL.find((t) => t.key === active.value))
const currentProps = computed(() => {
  if (active.value === 'creation') return { ...current.value.props, resetKey: createRequestKey.value }
  if (active.value === 'history') return { ...current.value.props, selectedSid: selectedHistorySid.value }
  return current.value.props || {}
})
const filteredSidebarSessions = computed(() => {
  const query = sidebarQuery.value.trim().toLocaleLowerCase()
  const filtered = query
    ? sidebarSessions.value.filter((item) => `${item.idea || ''} ${item.session_id || ''}`.toLocaleLowerCase().includes(query))
    : sidebarSessions.value
  return filtered.slice(0, 30)
})

async function loadSidebarSessions() {
  try {
    const data = await api('GET', '/api/production')
    sidebarSessions.value = data.sessions || []
    sidebarListMsg.value = sidebarSessions.value.length ? '' : '暂无历史创作'
  } catch (e) {
    sidebarListMsg.value = '历史创作加载失败'
  }
}
onMounted(() => {
  loadSidebarSessions()
  api('GET', '/api/app-settings').then((data) => applyTheme(data.theme)).catch(() => {})
})

function activateTab(key) {
  if (key === 'creation') createRequestKey.value++
  if (key === 'history') selectedHistorySid.value = null
  active.value = key
}
function openHistoryProject(sid) {
  selectedHistorySid.value = sid
  active.value = 'history'
}
function syncHistorySelection(sid) {
  selectedHistorySid.value = sid || null
}
function sidebarTitle(session) {
  if (session.series_id) {
    const series = session.series_title || '连续短剧'
    const number = session.episode_number ? `第${session.episode_number}集` : '剧集'
    const title = session.episode_title ? `：${session.episode_title}` : ''
    return `${series} · ${number}${title}`
  }
  return session.idea || ('未命名创作 ' + session.session_id)
}
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <div class="brand"><span class="logo">S</span><span>SceneForge<small>AI 短视频工作台</small></span></div>
      <label class="side-search">
        <Search :size="16" />
        <input v-model="sidebarQuery" type="search" placeholder="搜索创作" aria-label="搜索最近创作" />
      </label>
      <nav class="primary-nav">
        <button v-for="t in TABS" :key="t.key" :class="[{ active: active === t.key }, t.key === 'history' ? 'history-tab' : '']" @click="activateTab(t.key)">
          <component :is="t.icon" class="ic" :size="17" />{{ t.label }}
        </button>
      </nav>
      <section class="side-history" aria-label="最近历史创作">
        <div class="side-history-head">
          <span>历史创作</span>
          <button type="button" :class="{ active: active === 'history' && !selectedHistorySid }" @click="activateTab('history')">
            查看全部<ChevronRight :size="13" />
          </button>
        </div>
        <div class="side-history-list" aria-live="polite">
          <div v-if="sidebarListMsg" class="side-history-empty">{{ sidebarListMsg }}</div>
          <div v-else-if="!filteredSidebarSessions.length" class="side-history-empty">没有匹配的创作</div>
          <button v-for="session in filteredSidebarSessions" :key="session.session_id" type="button" class="side-project-item"
            :class="{ active: active === 'history' && selectedHistorySid === session.session_id }"
            :title="sidebarTitle(session)" @click="openHistoryProject(session.session_id)">
            <span class="side-project-icon"><Clapperboard :size="14" /></span>
            <span class="side-project-copy">
              <strong>{{ sidebarTitle(session) }}</strong>
              <small>{{ stageLabel(session) }}</small>
            </span>
          </button>
        </div>
      </section>
      <nav class="nav-bottom">
        <button v-for="t in BOTTOM" :key="t.key" :class="{ active: active === t.key }" @click="activateTab(t.key)">
          <component :is="t.icon" class="ic" :size="17" />{{ t.label }}
        </button>
      </nav>
      <div class="side-foot">本地控制台 · 127.0.0.1</div>
    </aside>
    <main class="content" :class="{ wide: current.comp === Production || current.comp === Creation }">
      <h1 v-if="current.comp !== Production && current.comp !== Creation" class="page-title">{{ current.label }}</h1>
      <!-- keep each view alive so in-flight state (polling, forms) survives tab switches -->
      <keep-alive>
        <component :is="current.comp" v-bind="currentProps" :key="current.key"
          @sessions-changed="loadSidebarSessions" @history-selection="syncHistorySelection" @open-settings="activateTab('config')" />
      </keep-alive>
    </main>
    <ConfirmModal />
    <Lightbox />
  </div>
</template>
