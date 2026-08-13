<script setup>
import { onMounted, ref } from 'vue'
import { Check, CheckCircle2, FolderOpen, Moon, RefreshCw, RotateCcw, Save, Sun, TriangleAlert } from '@lucide/vue'
import { api } from '../lib/api.js'
import { applyTheme } from '../lib/theme.js'

const theme = ref('light')
const mediaRoot = ref('')
const defaultMediaRoot = ref('')
const loading = ref(true)
const saving = ref(false)
const pickingFolder = ref(false)
const msg = ref('')
const readiness = ref(null)
const checkingReadiness = ref(false)

async function checkReadiness() {
  checkingReadiness.value = true
  try { readiness.value = await api('GET', '/api/app-settings/readiness') }
  catch (e) { readiness.value = { ready: false, summary: '自检失败：' + e.message, checks: [] } }
  checkingReadiness.value = false
}

async function load() {
  loading.value = true
  try {
    const data = await api('GET', '/api/app-settings')
    theme.value = data.theme || 'light'
    mediaRoot.value = data.media_root || ''
    defaultMediaRoot.value = data.default_media_root || ''
    applyTheme(theme.value)
  } catch (e) {
    msg.value = '加载失败：' + e.message
  }
  loading.value = false
}

function chooseTheme(value) {
  theme.value = value
  applyTheme(value)
}

function resetMediaRoot() {
  mediaRoot.value = defaultMediaRoot.value
  msg.value = '已恢复默认目录，保存后生效'
}

async function chooseMediaRoot() {
  pickingFolder.value = true
  msg.value = '正在打开系统文件夹选择器…'
  try {
    const data = await api('POST', '/api/app-settings/directory-picker', {
      initial_directory: mediaRoot.value.trim(),
    })
    if (data.selected && data.path) {
      mediaRoot.value = data.path
      msg.value = '已选择文件夹，保存后生效'
    } else {
      msg.value = '已取消选择'
    }
  } catch (e) {
    msg.value = '无法选择文件夹：' + ((e.body && e.body.error) || e.message)
  }
  pickingFolder.value = false
}

async function save() {
  saving.value = true
  msg.value = '保存中…'
  try {
    const data = await api('PUT', '/api/app-settings', { theme: theme.value, media_root: mediaRoot.value.trim() })
    theme.value = data.theme
    mediaRoot.value = data.media_root
    defaultMediaRoot.value = data.default_media_root
    applyTheme(theme.value)
    window.dispatchEvent(new CustomEvent('sceneforge-theme-changed', { detail: theme.value }))
    msg.value = '设置已保存'
  } catch (e) {
    msg.value = '保存失败：' + ((e.body && e.body.error) || e.message)
  }
  saving.value = false
}

onMounted(() => { load(); checkReadiness() })
</script>

<template>
  <div v-if="loading" class="panel muted">加载中…</div>
  <template v-else>
    <section class="panel general-settings-panel readiness-panel">
      <div class="setting-section-head">
        <div><h2>创作自检</h2><span>开始生成前检查本地环境和核心模型配置，不会显示或发送密钥。</span></div>
        <button class="ghost" type="button" :disabled="checkingReadiness" @click="checkReadiness"><RefreshCw :size="15" />{{ checkingReadiness ? '检查中…' : '重新检查' }}</button>
      </div>
      <div v-if="readiness" class="readiness-summary" :class="{ ready: readiness.ready }">
        <CheckCircle2 v-if="readiness.ready" :size="18" /><TriangleAlert v-else :size="18" />
        <strong>{{ readiness.summary }}</strong>
      </div>
      <div v-if="readiness" class="readiness-grid">
        <div v-for="item in readiness.checks" :key="item.key" class="readiness-check" :class="item.status">
          <CheckCircle2 v-if="item.status === 'ok'" :size="15" /><TriangleAlert v-else :size="15" />
          <div><strong>{{ item.label }}</strong><span>{{ item.detail }}</span></div>
        </div>
      </div>
    </section>

    <section class="panel general-settings-panel">
      <div class="setting-section-head"><div><h2>界面主题</h2><span>选择后立即预览，保存后在下次启动时保留。</span></div></div>
      <div class="theme-picker" role="group" aria-label="界面主题">
        <button type="button" :class="{ active: theme === 'light' }" :aria-pressed="theme === 'light'" @click="chooseTheme('light')">
          <span class="theme-icon"><Sun :size="18" /></span>
          <span class="theme-copy"><strong>明亮</strong><small>清晰明快，适合白天</small></span>
          <span v-if="theme === 'light'" class="theme-selected"><Check :size="14" />当前</span>
        </button>
        <button type="button" :class="{ active: theme === 'dark' }" :aria-pressed="theme === 'dark'" @click="chooseTheme('dark')">
          <span class="theme-icon"><Moon :size="18" /></span>
          <span class="theme-copy"><strong>暗色</strong><small>低亮舒适，适合夜间</small></span>
          <span v-if="theme === 'dark'" class="theme-selected"><Check :size="14" />当前</span>
        </button>
      </div>
    </section>

    <section class="panel general-settings-panel">
      <div class="setting-section-head"><div><h2>媒体存储目录</h2><span>新建项目生成的图片、视频、音频和中间文件将保存在这里。</span></div></div>
      <label for="media-storage-root">存储位置</label>
      <div class="storage-path-row">
        <div class="storage-path-input">
          <FolderOpen :size="17" aria-hidden="true" />
          <input id="media-storage-root" v-model="mediaRoot" type="text" placeholder="例如 D:\\SceneForgeMedia" spellcheck="false" />
        </div>
        <button class="ghost storage-browse" type="button" :disabled="pickingFolder" @click="chooseMediaRoot">
          <FolderOpen :size="16" />{{ pickingFolder ? '选择中…' : '选择文件夹' }}
        </button>
        <button class="ghost icon-action" type="button" title="恢复默认目录" aria-label="恢复默认目录" @click="resetMediaRoot">
          <RotateCcw :size="16" />
        </button>
      </div>
      <div class="storage-note">可以直接输入绝对路径，也可以从电脑中选择。更换目录不会移动已有项目，保存时会检查目录是否可写。</div>
    </section>

    <div class="settings-savebar">
      <span class="muted" role="status">{{ msg }}</span>
      <button class="act" type="button" :disabled="saving" @click="save"><Save :size="15" />{{ saving ? '保存中…' : '保存常规设置' }}</button>
    </div>
  </template>
</template>
