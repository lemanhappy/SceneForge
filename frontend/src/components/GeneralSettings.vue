<script setup>
import { onMounted, ref } from 'vue'
import { FolderOpen, Moon, RotateCcw, Save, Sun } from '@lucide/vue'
import { api } from '../lib/api.js'
import { applyTheme } from '../lib/theme.js'

const theme = ref('light')
const mediaRoot = ref('')
const defaultMediaRoot = ref('')
const loading = ref(true)
const saving = ref(false)
const msg = ref('')

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

onMounted(load)
</script>

<template>
  <div v-if="loading" class="panel muted">加载中…</div>
  <template v-else>
    <section class="panel general-settings-panel">
      <div class="setting-section-head"><div><h2>界面主题</h2><span>切换后立即应用，并在下次启动时保留。</span></div></div>
      <div class="theme-picker" role="group" aria-label="界面主题">
        <button type="button" :class="{ active: theme === 'light' }" @click="chooseTheme('light')"><Sun :size="18" /><span><strong>明亮</strong><small>适合白天和高亮环境</small></span></button>
        <button type="button" :class="{ active: theme === 'dark' }" @click="chooseTheme('dark')"><Moon :size="18" /><span><strong>暗色</strong><small>适合夜间和长时间创作</small></span></button>
      </div>
    </section>

    <section class="panel general-settings-panel">
      <div class="setting-section-head"><div><h2>媒体存储目录</h2><span>新建项目生成的图片、视频、音频和中间文件将保存在这里。</span></div></div>
      <label for="media-storage-root">绝对路径</label>
      <div class="storage-path-row">
        <span class="storage-path-icon"><FolderOpen :size="17" /></span>
        <input id="media-storage-root" v-model="mediaRoot" type="text" placeholder="例如 D:\\SceneForgeMedia" spellcheck="false" />
        <button class="ghost" type="button" title="恢复默认目录" @click="resetMediaRoot"><RotateCcw :size="15" />恢复默认</button>
      </div>
      <div class="storage-note">更换目录不会移动已有项目；历史创作仍从原目录读取。目录不存在时会自动创建，并在保存时检查是否可写。</div>
    </section>

    <div class="settings-savebar">
      <span class="muted" role="status">{{ msg }}</span>
      <button class="act" type="button" :disabled="saving" @click="save"><Save :size="15" />{{ saving ? '保存中…' : '保存常规设置' }}</button>
    </div>
  </template>
</template>
