<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowLeft, BookOpen, CheckCircle2, ChevronRight, Clock3, Film, Plus, Save, Settings2, Trash2 } from '@lucide/vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { stageInfo, stageLabel } from '../lib/stages.js'
import CreateForm from '../components/CreateForm.vue'
import SessionDetail from '../components/SessionDetail.vue'

const emit = defineEmits(['sessions-changed'])
const seriesList = ref([])
const selectedId = ref('')
const detail = ref(null)
const pageState = ref('list')
const loading = ref(true)
const saving = ref(false)
const msg = ref('')
const catalogs = reactive({ characters: [], props: [], scenes: [], loras: [] })
const draft = reactive(emptyDraft())

function emptyDraft() {
  return {
    title: '', premise: '', planned_episode_count: 12, episode_duration_sec: 60,
    style: '', target_language: 'zh-CN', aspect_ratio: 'portrait', quality_tier: 'balanced',
    character_asset_ids: [], prop_asset_ids: [], scene_asset_ids: [], lora_ids: [], bible_notes: '',
  }
}

function resetDraft(value = null) {
  Object.assign(draft, emptyDraft())
  if (!value) return
  Object.assign(draft, {
    title: value.title || '', premise: value.premise || '',
    planned_episode_count: value.planned_episode_count || 12,
    episode_duration_sec: value.episode_duration_sec || 60,
    style: value.style || '', target_language: value.target_language || 'zh-CN',
    aspect_ratio: value.aspect_ratio || 'portrait', quality_tier: value.quality_tier || 'balanced',
    character_asset_ids: [...(value.character_asset_ids || [])],
    prop_asset_ids: [...(value.prop_asset_ids || [])],
    scene_asset_ids: [...(value.scene_asset_ids || [])],
    lora_ids: [...(value.lora_ids || [])],
    bible_notes: (value.bible && value.bible.notes) || '',
  })
}

async function loadSeries() {
  loading.value = true
  try {
    const result = await api('GET', '/api/series')
    seriesList.value = result.series || []
    msg.value = ''
  } catch (e) { msg.value = '加载失败：' + e.message }
  loading.value = false
}

async function loadCatalogs() {
  const results = await Promise.allSettled([
    api('GET', '/api/characters'), api('GET', '/api/assets?asset_type=prop'),
    api('GET', '/api/assets?asset_type=scene'), api('GET', '/api/loras'),
  ])
  if (results[0].status === 'fulfilled') catalogs.characters = results[0].value.characters || []
  if (results[1].status === 'fulfilled') catalogs.props = results[1].value.assets || []
  if (results[2].status === 'fulfilled') catalogs.scenes = results[2].value.assets || []
  if (results[3].status === 'fulfilled') catalogs.loras = (results[3].value.loras || []).filter((item) => item.enabled)
}

onMounted(() => { loadSeries(); loadCatalogs() })

function startCreate() { resetDraft(); pageState.value = 'create'; msg.value = '' }
function backToList() { selectedId.value = ''; detail.value = null; pageState.value = 'list'; loadSeries() }

async function openSeries(seriesId) {
  selectedId.value = seriesId
  pageState.value = 'detail'
  try { detail.value = await api('GET', '/api/series/' + seriesId); msg.value = '' }
  catch (e) { msg.value = '加载失败：' + e.message }
}

async function saveSeries() {
  if (!draft.title.trim()) { msg.value = '请填写短剧名称'; return }
  saving.value = true
  const payload = {
    ...draft,
    title: draft.title.trim(), premise: draft.premise.trim(), style: draft.style.trim(),
    bible: draft.bible_notes.trim() ? { notes: draft.bible_notes.trim() } : {},
  }
  delete payload.bible_notes
  try {
    if (selectedId.value && pageState.value === 'settings') {
      await api('PUT', '/api/series/' + selectedId.value, payload)
      await openSeries(selectedId.value)
      msg.value = '作品设置已保存'
    } else {
      const created = await api('POST', '/api/series', payload)
      await loadSeries()
      await openSeries(created.series_id)
    }
  } catch (e) { msg.value = '保存失败：' + ((e.body && e.body.error) || e.message) }
  saving.value = false
}

function editSeries() { resetDraft(detail.value); pageState.value = 'settings'; msg.value = '' }

async function deleteSeries() {
  if (!detail.value || detail.value.episode_count) return
  if (!await confirmModal(`删除短剧「${detail.value.title}」？`, { okText: '删除', danger: true })) return
  try { await api('DELETE', '/api/series/' + detail.value.series_id); backToList() }
  catch (e) { msg.value = '删除失败：' + ((e.body && e.body.error) || e.message) }
}

function startEpisode() {
  if (!detail.value || detail.value.next_episode_number > detail.value.planned_episode_count) return
  pageState.value = 'episode-create'
  msg.value = ''
}

function openEpisode(sessionId) { selectedId.value = detail.value.series_id; pageState.value = 'episode'; activeEpisodeSid.value = sessionId }
const activeEpisodeSid = ref('')

async function onEpisodeCreated(sessionId) {
  activeEpisodeSid.value = sessionId
  await openSeries(selectedId.value)
  pageState.value = 'episode'
  emit('sessions-changed')
}

async function onEpisodeChanged() {
  if (selectedId.value) detail.value = await api('GET', '/api/series/' + selectedId.value)
  emit('sessions-changed')
}

const episodeContext = computed(() => {
  const item = detail.value
  if (!item) return null
  const number = item.next_episode_number
  const outline = (item.outline || []).find((entry) => Number(entry.episode_number) === Number(number)) || {}
  return {
    series_id: item.series_id, series_title: item.title, episode_number: number,
    episode_title: outline.title || '', episode_outline: outline.synopsis || outline.outline || '',
    episode_duration_sec: item.episode_duration_sec, style: item.style,
    target_language: item.target_language, aspect_ratio: item.aspect_ratio,
    quality_tier: item.quality_tier, domain: item.domain,
    character_asset_ids: item.character_asset_ids || [], prop_asset_ids: item.prop_asset_ids || [],
    scene_asset_ids: item.scene_asset_ids || [], lora_ids: item.lora_ids || [],
  }
})

function progressPercent(item) {
  if (!item || !item.planned_episode_count) return 0
  return Math.round((item.completed_episode_count || 0) / item.planned_episode_count * 100)
}
function episodePercent(episode) {
  const info = stageInfo(episode)
  return info.phase === 'done' ? 100 : Math.min(95, info.idx * 25 + (info.phase === 'review' ? 20 : info.phase === 'generating' ? 10 : 0))
}
function isComplete(episode) { return ['completed', 'published'].includes(episode.stage) }
function toggleAsset(field, id) {
  const values = draft[field]
  const index = values.indexOf(id)
  if (index >= 0) values.splice(index, 1)
  else values.push(id)
}
</script>

<template>
  <div v-if="pageState === 'episode-create' && episodeContext" class="series-embedded-workflow">
    <CreateForm :key="`${episodeContext.series_id}-${episodeContext.episode_number}`" :series-context="episodeContext"
      @created="onEpisodeCreated" @sessions-changed="emit('sessions-changed')" @cancel="openSeries(selectedId)" />
  </div>

  <div v-else-if="pageState === 'episode' && detail" class="series-embedded-workflow">
    <div class="series-episode-contextbar">
      <button class="ghost" type="button" @click="openSeries(selectedId)"><ArrowLeft :size="15" />剧集列表</button>
      <span><strong>{{ detail.title }}</strong><small>第 {{ (detail.episodes.find((item) => item.session_id === activeEpisodeSid) || {}).episode_number }} 集</small></span>
    </div>
    <SessionDetail :sid="activeEpisodeSid" @sessions-changed="onEpisodeChanged" />
  </div>

  <div v-else class="series-page">
    <template v-if="pageState === 'list'">
      <header class="series-page-head">
        <div><h1>连续短剧</h1><p>统一管理作品设定，并逐集完成剧本、分镜、镜头和成片。</p></div>
        <button class="act" type="button" @click="startCreate"><Plus :size="16" />新建短剧</button>
      </header>
      <div v-if="loading" class="series-empty">加载中…</div>
      <div v-else-if="!seriesList.length" class="series-empty"><BookOpen :size="28" /><strong>还没有连续短剧</strong><span>建立作品后，再按集制作，角色和场景会自动延续。</span></div>
      <div v-else class="series-list">
        <button v-for="item in seriesList" :key="item.series_id" class="series-row" type="button" @click="openSeries(item.series_id)">
          <span class="series-cover"><Film :size="20" /></span>
          <span class="series-row-main"><strong>{{ item.title }}</strong><small>{{ item.premise || '尚未填写故事简介' }}</small></span>
          <span class="series-row-progress"><span>{{ item.completed_episode_count }}/{{ item.planned_episode_count }} 集</span><i><b :style="{ width: progressPercent(item) + '%' }"></b></i></span>
          <span class="series-row-state">{{ item.episode_count ? '制作中' : '待创建首集' }}</span>
          <ChevronRight :size="17" />
        </button>
      </div>
    </template>

    <template v-else-if="pageState === 'create' || pageState === 'settings'">
      <header class="series-page-head compact">
        <button class="iconbtn" type="button" title="返回" aria-label="返回" @click="pageState === 'settings' ? openSeries(selectedId) : backToList()"><ArrowLeft :size="18" /></button>
        <div><h1>{{ pageState === 'settings' ? '作品设置' : '新建连续短剧' }}</h1><p>作品级设置会作为每一集的默认值和一致性约束。</p></div>
      </header>
      <section class="series-form">
        <div class="series-form-grid">
          <div class="wide"><label>短剧名称</label><input v-model="draft.title" placeholder="例如：雨夜归途" /></div>
          <div><label>计划集数</label><input v-model.number="draft.planned_episode_count" type="number" min="1" max="200" /></div>
          <div><label>单集时长</label><div class="series-unit-input"><input v-model.number="draft.episode_duration_sec" type="number" min="5" max="600" /><span>秒</span></div></div>
          <div class="wide"><label>故事简介</label><textarea v-model="draft.premise" placeholder="主角是谁、核心冲突是什么、整部短剧要讲到哪里"></textarea></div>
          <div class="wide"><label>统一视觉风格</label><input v-model="draft.style" placeholder="例如：现实主义都市雨夜，冷蓝环境光与暖黄室内灯对比" /></div>
          <div><label>画面比例</label><select v-model="draft.aspect_ratio"><option value="portrait">竖屏 9:16</option><option value="landscape">横屏 16:9</option><option value="square">方形 1:1</option></select></div>
          <div><label>质量档位</label><select v-model="draft.quality_tier"><option value="economy">省钱</option><option value="balanced">均衡</option><option value="quality">高质量</option></select></div>
          <div class="wide"><label>作品设定</label><textarea v-model="draft.bible_notes" placeholder="人物关系、世界规则、不可改变的事实和贯穿全剧的视觉要求"></textarea></div>
        </div>
        <div class="series-assets">
          <div><label>固定角色</label><span v-if="!catalogs.characters.length" class="muted">暂无角色模型</span><div class="series-asset-options"><button v-for="item in catalogs.characters" :key="item.asset_id" type="button" :class="{ active: draft.character_asset_ids.includes(item.asset_id) }" @click="toggleAsset('character_asset_ids', item.asset_id)">{{ item.display_name || item.asset_id }}</button></div></div>
          <div><label>固定场景</label><span v-if="!catalogs.scenes.length" class="muted">暂无场景模型</span><div class="series-asset-options"><button v-for="item in catalogs.scenes" :key="item.asset_id" type="button" :class="{ active: draft.scene_asset_ids.includes(item.asset_id) }" @click="toggleAsset('scene_asset_ids', item.asset_id)">{{ item.display_name || item.asset_id }}</button></div></div>
          <div><label>固定道具</label><span v-if="!catalogs.props.length" class="muted">暂无道具模型</span><div class="series-asset-options"><button v-for="item in catalogs.props" :key="item.asset_id" type="button" :class="{ active: draft.prop_asset_ids.includes(item.asset_id) }" @click="toggleAsset('prop_asset_ids', item.asset_id)">{{ item.display_name || item.asset_id }}</button></div></div>
        </div>
        <div class="series-form-actions"><span class="muted">{{ msg }}</span><button class="act" type="button" :disabled="saving" @click="saveSeries"><Save :size="16" />{{ saving ? '保存中…' : (pageState === 'settings' ? '保存作品设置' : '创建短剧') }}</button></div>
      </section>
    </template>

    <template v-else-if="detail">
      <header class="series-detail-head">
        <button class="iconbtn" type="button" title="返回短剧列表" aria-label="返回短剧列表" @click="backToList"><ArrowLeft :size="18" /></button>
        <div class="series-detail-copy"><span>连续短剧</span><h1>{{ detail.title }}</h1><p>{{ detail.premise || '尚未填写故事简介' }}</p></div>
        <div class="series-detail-actions"><button class="ghost" type="button" @click="editSeries"><Settings2 :size="16" />作品设置</button><button class="act" type="button" :disabled="detail.next_episode_number > detail.planned_episode_count" @click="startEpisode"><Plus :size="16" />创建第 {{ detail.next_episode_number }} 集</button></div>
      </header>
      <div class="series-summary-band">
        <div><span>制作进度</span><strong>{{ detail.completed_episode_count }} / {{ detail.planned_episode_count }} 集</strong><i><b :style="{ width: progressPercent(detail) + '%' }"></b></i></div>
        <div><span>单集规格</span><strong>{{ detail.episode_duration_sec }} 秒 · {{ detail.aspect_ratio === 'portrait' ? '竖屏 9:16' : detail.aspect_ratio === 'square' ? '方形 1:1' : '横屏 16:9' }}</strong></div>
        <div><span>固定资产</span><strong>{{ (detail.character_asset_ids || []).length }} 角色 · {{ (detail.scene_asset_ids || []).length }} 场景 · {{ (detail.prop_asset_ids || []).length }} 道具</strong></div>
      </div>
      <section class="episode-section">
        <div class="episode-section-head"><div><h2>剧集</h2><span>每集独立制作，下一集自动继承上一集的收尾状态。</span></div></div>
        <div class="episode-table">
          <div class="episode-table-head"><span>集数</span><span>标题与内容</span><span>制作进度</span><span>操作</span></div>
          <button v-for="episode in detail.episodes" :key="episode.session_id" class="episode-row" type="button" @click="openEpisode(episode.session_id)">
            <span class="episode-number">{{ String(episode.episode_number).padStart(2, '0') }}</span>
            <span class="episode-copy"><strong>{{ episode.episode_title || `第 ${episode.episode_number} 集` }}</strong><small>{{ episode.idea || '尚未填写本集简介' }}</small></span>
            <span class="episode-progress"><span><CheckCircle2 v-if="isComplete(episode)" :size="15" /><Clock3 v-else :size="15" />{{ stageLabel(episode) }}</span><i><b :style="{ width: episodePercent(episode) + '%' }"></b></i></span>
            <span class="episode-action">{{ isComplete(episode) ? '查看成片' : '继续制作' }}<ChevronRight :size="15" /></span>
          </button>
          <button v-if="detail.next_episode_number <= detail.planned_episode_count" class="episode-add-row" type="button" @click="startEpisode"><Plus :size="16" />创建第 {{ detail.next_episode_number }} 集</button>
          <div v-else class="episode-complete-note"><CheckCircle2 :size="17" />计划内剧集已全部创建</div>
        </div>
      </section>
      <div v-if="!detail.episode_count" class="series-danger-zone"><button class="ghost danger-text" type="button" @click="deleteSeries"><Trash2 :size="15" />删除空作品</button></div>
      <div v-if="msg" class="series-page-message">{{ msg }}</div>
    </template>
  </div>
</template>
