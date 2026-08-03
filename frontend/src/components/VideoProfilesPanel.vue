<script setup>
import { computed, reactive, ref } from 'vue'
import { CircleCheck, Plus, Save, Trash2 } from '@lucide/vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'

const props = defineProps({ initial: Object })
const state = ref(props.initial || { profiles: [], model_catalog: {}, base_url_options: [] })
const drafts = reactive({})
const createOpen = ref(false)
const newId = ref('')
const newLabel = ref('')
const msg = ref('')

const modelOptions = computed(() => Object.values(state.value.model_catalog || {}).flat())

function draft(profile) {
  if (!drafts[profile.profile_id]) {
    drafts[profile.profile_id] = {
      ...profile,
      api_key_value: '',
      durations_text: (profile.supported_durations || []).join(', '),
      aspects_text: (profile.supported_aspect_ratios || []).join(', '),
    }
  }
  return drafts[profile.profile_id]
}

function replaceState(next) {
  state.value = next
  for (const key of Object.keys(drafts)) delete drafts[key]
}

function payload(item) {
  return {
    label: item.label,
    enabled: !!item.enabled,
    provider: item.provider,
    transport: item.transport,
    model: item.model,
    base_url: item.base_url,
    api_key: item.api_key_value,
    quality_tier: item.quality_tier,
    estimated_cost: item.estimated_cost,
    supported_durations: item.durations_text.split(',').map((value) => value.trim()).filter(Boolean),
    supported_aspect_ratios: item.aspects_text.split(',').map((value) => value.trim()).filter(Boolean),
    max_reference_count: Number(item.max_reference_count || 0),
    remote_cancel: !!item.remote_cancel,
    capabilities: { ...item.capabilities },
  }
}

async function save(profile) {
  const item = draft(profile)
  msg.value = '保存中…'
  try {
    replaceState(await api('PUT', '/api/config/video-profiles/' + encodeURIComponent(profile.profile_id), payload(item)))
    msg.value = '视频模型配置已保存'
  } catch (e) { msg.value = '保存失败：' + e.message }
}

async function createProfile() {
  const profileId = newId.value.trim()
  if (!profileId) { msg.value = '请填写配置 ID'; return }
  msg.value = '创建中…'
  try {
    const firstModel = modelOptions.value[0] || ''
    replaceState(await api('POST', '/api/config/video-profiles', {
      profile_id: profileId,
      label: newLabel.value.trim() || profileId,
      enabled: true,
      provider: firstModel.includes('seedance') ? 'seedance' : '',
      model: firstModel,
      base_url: (state.value.base_url_options || [])[0] || '',
      quality_tier: 'balanced',
      supported_aspect_ratios: ['landscape', 'portrait', 'square'],
      supported_durations: firstModel.includes('seedance') ? [5, 10] : [8],
      max_reference_count: 2,
    }))
    newId.value = ''
    newLabel.value = ''
    createOpen.value = false
    msg.value = '视频模型配置已创建'
  } catch (e) { msg.value = '创建失败：' + e.message }
}

async function activate(profile) {
  try {
    replaceState(await api('POST', '/api/config/video-profiles/' + encodeURIComponent(profile.profile_id) + '/activate'))
    msg.value = '已设为默认视频模型'
  } catch (e) { msg.value = '设置失败：' + e.message }
}

async function remove(profile) {
  if (!await confirmModal('删除视频模型配置「' + profile.label + '」？', { okText: '删除', danger: true })) return
  try {
    replaceState(await api('DELETE', '/api/config/video-profiles/' + encodeURIComponent(profile.profile_id)))
    msg.value = '配置已删除'
  } catch (e) { msg.value = '删除失败：' + e.message }
}
</script>

<template>
  <section class="video-profiles">
    <div class="profile-heading">
      <div>
        <h3>视频模型配置</h3>
        <p>可同时维护多个模型。生成前按项目要求、质量档位和成本选择，路由结果会固定到项目供返工复用。</p>
      </div>
      <button class="ghost" type="button" @click="createOpen = !createOpen"><Plus :size="16" />新增配置</button>
    </div>

    <div v-if="createOpen" class="profile-create">
      <div><label>配置 ID</label><input v-model="newId" placeholder="seedance-main" /></div>
      <div><label>显示名称</label><input v-model="newLabel" placeholder="Seedance 主线路" /></div>
      <button class="act" type="button" @click="createProfile"><Plus :size="16" />创建</button>
    </div>

    <article v-for="profile in state.profiles" :key="profile.profile_id" class="profile-editor">
      <div class="profile-title">
        <div><strong>{{ profile.label }}</strong><span>{{ profile.profile_id }}</span></div>
        <span v-if="state.default_profile_id === profile.profile_id" class="tag"><CircleCheck :size="13" />默认</span>
      </div>
      <div class="profile-grid">
        <div><label>名称</label><input v-model="draft(profile).label" /></div>
        <div><label>模型</label><input v-model="draft(profile).model" :list="'video-models-' + profile.profile_id" /></div>
        <datalist :id="'video-models-' + profile.profile_id"><option v-for="model in modelOptions" :key="model" :value="model" /></datalist>
        <div><label>模型厂商</label><input v-model="draft(profile).provider" placeholder="seedance / veo" /></div>
        <div><label>API 通道</label><input v-model="draft(profile).transport" placeholder="yunwu / openrouter" /></div>
        <div class="wide"><label>API 地址</label><input v-model="draft(profile).base_url" :list="'video-urls-' + profile.profile_id" /></div>
        <datalist :id="'video-urls-' + profile.profile_id"><option v-for="url in state.base_url_options" :key="url" :value="url" /></datalist>
        <div class="wide"><label>API Key <span v-if="profile.api_key && profile.api_key.set" class="tag">已设置 {{ profile.api_key.hint }}{{ profile.api_key_inherited ? ' · 继承' : '' }}</span></label><input v-model="draft(profile).api_key_value" type="password" placeholder="留空不改" /></div>
        <div><label>质量档位</label><select v-model="draft(profile).quality_tier"><option value="economy">省钱</option><option value="balanced">均衡</option><option value="quality">高质量</option></select></div>
        <div><label>单镜估算成本</label><input v-model="draft(profile).estimated_cost" type="number" min="0" step="0.01" placeholder="可选" /></div>
        <div><label>支持时长（秒）</label><input v-model="draft(profile).durations_text" placeholder="5, 10" /></div>
        <div><label>最大参考图数</label><input v-model="draft(profile).max_reference_count" type="number" min="0" /></div>
        <div class="wide"><label>支持画幅</label><input v-model="draft(profile).aspects_text" placeholder="landscape, portrait, square" /></div>
      </div>
      <div class="capability-row">
        <label><input v-model="draft(profile).enabled" type="checkbox" />启用</label>
        <label><input v-model="draft(profile).capabilities.text_to_video" type="checkbox" />文生视频</label>
        <label><input v-model="draft(profile).capabilities.image_to_video" type="checkbox" />图生视频</label>
        <label><input v-model="draft(profile).capabilities.first_last_frame" type="checkbox" />首尾帧</label>
        <label><input v-model="draft(profile).capabilities.multi_reference" type="checkbox" />多参考图</label>
      </div>
      <div class="profile-actions">
        <button class="act" type="button" @click="save(profile)"><Save :size="15" />保存</button>
        <button v-if="state.default_profile_id !== profile.profile_id" class="ghost" type="button" :disabled="!draft(profile).enabled" @click="activate(profile)"><CircleCheck :size="15" />设为默认</button>
        <button class="ghost danger-text" type="button" @click="remove(profile)"><Trash2 :size="15" />删除</button>
      </div>
    </article>
    <div class="muted profile-message">{{ msg }}</div>
  </section>
</template>

<style scoped>
.video-profiles { margin-top:18px; }
.profile-heading, .profile-title, .profile-actions, .capability-row { display:flex; align-items:center; gap:10px; }
.profile-heading { justify-content:space-between; margin-bottom:12px; }
.profile-heading h3 { margin:0; font-size:15px; }
.profile-heading p { margin:4px 0 0; color:var(--mut); font-size:12px; }
.profile-create { display:grid; grid-template-columns:1fr 1fr auto; gap:10px; align-items:end; padding:12px; border:1px solid var(--line); border-radius:7px; margin-bottom:12px; }
.profile-editor { padding:14px; border:1px solid var(--line); border-radius:7px; margin-top:10px; background:#fff; }
.profile-title { justify-content:space-between; margin-bottom:12px; }
.profile-title strong, .profile-title span { display:block; }
.profile-title span { margin-top:2px; color:var(--mut); font-size:11px; }
.profile-title .tag { display:flex; color:var(--ok); }
.profile-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.profile-grid .wide { grid-column:1 / -1; }
.profile-grid label, .profile-create label { display:block; margin-bottom:4px; font-size:11px; color:var(--mut); }
.profile-grid input, .profile-grid select, .profile-create input { width:100%; box-sizing:border-box; }
.capability-row { flex-wrap:wrap; margin-top:12px; font-size:12px; }
.capability-row label { display:flex; align-items:center; gap:5px; }
.profile-actions { margin-top:12px; }
.profile-actions button, .profile-heading button, .profile-create button { display:inline-flex; align-items:center; gap:6px; }
.profile-heading button { flex:0 0 auto; white-space:nowrap; }
.danger-text { color:var(--bad); margin-left:auto; }
.profile-message { min-height:18px; margin-top:10px; }
@media (max-width:700px) {
  .profile-grid, .profile-create { grid-template-columns:1fr; }
  .profile-grid .wide { grid-column:auto; }
  .profile-heading { align-items:flex-start; flex-direction:column; }
}
</style>
