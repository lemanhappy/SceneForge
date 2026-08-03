<script setup>
import { ref, watch } from 'vue'
import { api, mediaUrl } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { openLightbox } from '../lib/lightbox.js'

const props = defineProps({ char: Object })
const emit = defineEmits(['reload'])

const msg = ref('')
const histOpen = ref(false)
const hist = ref([]) // [{view, versions:[{version}]}]
const advancedOpen = ref(false)
const savingAdvanced = ref(false)
const identity = ref({ facial_features: '', hairstyle: '', body_features: '', age_range: '', signature_features: '', forbidden_changes: '' })
const bible = ref({
  personality_traits: '', behavioral_notes: '', continuity_notes: '',
  provider_voice_id: '', vocal_quality: '', speaking_style: '', accent: '', language: '',
  voice_forbidden_changes: '',
})
const lora = ref({ binding_id: '', enabled: false, provider: '', base_model: '', model_path: '', trigger_words: '', weight: 0.8 })
const t = Date.now()

function syncAdvanced(char) {
  const profile = char?.identity_profile || {}
  identity.value = {
    facial_features: profile.facial_features || '',
    hairstyle: profile.hairstyle || '',
    body_features: profile.body_features || '',
    age_range: profile.age_range || '',
    signature_features: (profile.signature_features || []).join(', '),
    forbidden_changes: (profile.forbidden_changes || []).join(', '),
  }
  const memory = char?.bible || {}
  const voice = memory.voice || {}
  bible.value = {
    personality_traits: (memory.personality_traits || []).join(', '),
    behavioral_notes: memory.behavioral_notes || '',
    continuity_notes: memory.continuity_notes || '',
    provider_voice_id: voice.provider_voice_id || '',
    vocal_quality: voice.vocal_quality || '',
    speaking_style: voice.speaking_style || '',
    accent: voice.accent || '',
    language: voice.language || '',
    voice_forbidden_changes: (voice.forbidden_changes || []).join(', '),
  }
  const binding = (char?.render_bindings || []).find((item) => item.kind === 'lora') || {}
  lora.value = {
    binding_id: binding.binding_id || '',
    enabled: Boolean(binding.enabled),
    provider: binding.provider || '',
    base_model: binding.base_model || '',
    model_path: binding.model_path || '',
    trigger_words: (binding.trigger_words || []).join(', '),
    weight: Number(binding.weight ?? 0.8),
  }
}

watch(() => props.char, syncAdvanced, { immediate: true, deep: true })

const viewKeys = () => Object.keys(props.char.views || {})
const imgUrl = (v) => mediaUrl('/api/characters/' + props.char.asset_id + '/image/' + v + '?t=' + t)

async function generate(view) {
  msg.value = '生成中…(调用图像模型)'
  try { await api('POST', '/api/characters/' + props.char.asset_id + '/generate', { view }); emit('reload') }
  catch (e) { msg.value = '失败：' + e.message }
}

async function del() {
  if (!await confirmModal('删除角色模型「' + props.char.asset_id + '」？', { okText: '删除', danger: true })) return
  try { await api('DELETE', '/api/characters/' + props.char.asset_id); emit('reload') }
  catch (e) { msg.value = '失败：' + e.message }
}

async function toggleHist() {
  if (histOpen.value) { histOpen.value = false; return }
  histOpen.value = true; hist.value = []
  for (const v of viewKeys()) {
    try {
      const data = await api('GET', '/api/characters/' + props.char.asset_id + '/versions/' + v)
      hist.value.push({ view: v, versions: data.versions || [] })
    } catch (e) { /* skip */ }
  }
}

const versionUrl = (v, ver) => mediaUrl('/api/characters/' + props.char.asset_id + '/version/' + v + '/' + ver)

async function rollback(view, version) {
  if (!await confirmModal('用 ' + view + ' v' + version + ' 覆盖当前画像？当前会先存为新历史版本。', { okText: '回滚' })) return
  try { await api('POST', '/api/characters/' + props.char.asset_id + '/rollback', { view, version: parseInt(version) }); emit('reload') }
  catch (e) { msg.value = '回滚失败：' + e.message }
}

const commaList = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean)

async function saveAdvanced() {
  savingAdvanced.value = true
  const otherBindings = (props.char.render_bindings || []).filter((item) => item.kind !== 'lora')
  const renderBindings = [...otherBindings]
  if (lora.value.binding_id.trim()) {
    renderBindings.push({
      kind: 'lora', binding_id: lora.value.binding_id.trim(), enabled: lora.value.enabled,
      provider: lora.value.provider.trim() || null, base_model: lora.value.base_model.trim(),
      model_path: lora.value.model_path.trim(), trigger_words: commaList(lora.value.trigger_words),
      weight: Number(lora.value.weight ?? 0.8),
    })
  }
  try {
    await api('POST', '/api/characters/' + props.char.asset_id, {
      identity_profile: {
        facial_features: identity.value.facial_features, hairstyle: identity.value.hairstyle,
        body_features: identity.value.body_features, age_range: identity.value.age_range,
        signature_features: commaList(identity.value.signature_features),
        forbidden_changes: commaList(identity.value.forbidden_changes),
      },
      bible: {
        personality_traits: commaList(bible.value.personality_traits),
        behavioral_notes: bible.value.behavioral_notes,
        continuity_notes: bible.value.continuity_notes,
        voice: {
          provider_voice_id: bible.value.provider_voice_id.trim() || null,
          vocal_quality: bible.value.vocal_quality,
          speaking_style: bible.value.speaking_style,
          accent: bible.value.accent,
          language: bible.value.language,
          forbidden_changes: commaList(bible.value.voice_forbidden_changes),
        },
      },
      render_bindings: renderBindings,
    })
    msg.value = '角色模型约束已保存 ✓'
    emit('reload')
  } catch (e) { msg.value = '保存失败：' + e.message }
  finally { savingAdvanced.value = false }
}

function removeLora() {
  lora.value = { binding_id: '', enabled: false, provider: '', base_model: '', model_path: '', trigger_words: '', weight: 0.8 }
}
</script>

<template>
  <div class="char">
    <div class="row" style="justify-content:space-between"><b>{{ char.display_name }} <span class="muted">({{ char.asset_id }})</span></b></div>
    <div class="muted">{{ char.description || '' }}</div>
    <div style="margin:8px 0">
      <template v-if="viewKeys().length">
        <img v-for="v in viewKeys()" :key="v" class="zoom" :title="v + '（点击放大）'" :src="imgUrl(v)" @click="openLightbox(imgUrl(v))" />
      </template>
      <span v-else class="muted">尚无画像</span>
    </div>
    <div class="row">
      <button class="ghost" @click="generate('front')">生成正面</button>
      <button class="ghost" @click="generate('side')">生成侧面</button>
      <button class="ghost" @click="generate('back')">生成背面</button>
      <button class="ghost" @click="toggleHist">历史版本</button>
      <button class="ghost" @click="advancedOpen = !advancedOpen">角色圣经</button>
      <button class="ghost" @click="del">删除</button>
      <span class="muted">{{ msg }}</span>
    </div>
    <div v-if="advancedOpen" class="advanced-bindings">
      <div class="advanced-title">身份约束</div>
      <div class="grid2">
        <div><label>面部特征</label><input v-model="identity.facial_features" /></div>
        <div><label>发型</label><input v-model="identity.hairstyle" /></div>
        <div><label>体态特征</label><input v-model="identity.body_features" /></div>
        <div><label>年龄范围</label><input v-model="identity.age_range" /></div>
      </div>
      <label>标志性特征（逗号分隔）</label><input v-model="identity.signature_features" />
      <label>禁止变化（逗号分隔）</label><input v-model="identity.forbidden_changes" />
      <div class="advanced-title">表演与连续性</div>
      <div class="grid2">
        <div><label>性格标签（逗号分隔）</label><input v-model="bible.personality_traits" /></div>
        <div><label>行为习惯</label><input v-model="bible.behavioral_notes" /></div>
      </div>
      <label>跨镜头连续性</label><input v-model="bible.continuity_notes" />
      <div class="advanced-title">声线</div>
      <div class="grid2">
        <div><label>声音 ID</label><input v-model="bible.provider_voice_id" /></div>
        <div><label>音色</label><input v-model="bible.vocal_quality" /></div>
        <div><label>说话方式</label><input v-model="bible.speaking_style" /></div>
        <div><label>口音</label><input v-model="bible.accent" /></div>
        <div><label>语言</label><input v-model="bible.language" /></div>
        <div><label>声线禁止变化</label><input v-model="bible.voice_forbidden_changes" /></div>
      </div>
      <div class="advanced-title">LoRA</div>
      <div class="grid2">
        <div><label>绑定 ID</label><input v-model="lora.binding_id" placeholder="lead_lora" /></div>
        <div><label>服务商</label><input v-model="lora.provider" placeholder="comfyui / cloud" /></div>
        <div><label>基础模型</label><input v-model="lora.base_model" placeholder="模型 ID" /></div>
        <div><label>模型路径或云端 ID</label><input v-model="lora.model_path" /></div>
        <div><label>触发词（逗号分隔）</label><input v-model="lora.trigger_words" /></div>
        <div><label>权重</label><input v-model.number="lora.weight" type="number" min="0" max="2" step="0.05" /></div>
      </div>
      <label class="toggle-line"><input v-model="lora.enabled" type="checkbox" :disabled="!lora.binding_id.trim()" /> 启用 LoRA</label>
      <div class="row advanced-actions">
        <button class="act" :disabled="savingAdvanced" @click="saveAdvanced">{{ savingAdvanced ? '保存中…' : '保存约束' }}</button>
        <button v-if="lora.binding_id" class="ghost" @click="removeLora">移除 LoRA</button>
      </div>
    </div>
    <div v-if="histOpen" style="margin-top:10px">
      <template v-if="hist.length">
        <div v-for="h in hist" :key="h.view">
          <div class="muted" style="margin-top:6px">{{ h.view }}（{{ h.versions.length }} 个历史版本）</div>
          <div class="row">
            <div v-for="ver in h.versions" :key="ver.version" style="text-align:center">
              <img class="zoom" style="height:84px;border-radius:8px;border:1px solid var(--line)" :src="versionUrl(h.view, ver.version)" @click="openLightbox(versionUrl(h.view, ver.version))" />
              <div><button class="ghost" style="margin-top:4px;padding:4px 8px" @click="rollback(h.view, ver.version)">回滚 v{{ ver.version }}</button></div>
            </div>
            <span v-if="!h.versions.length" class="muted">（暂无历史）</span>
          </div>
        </div>
      </template>
      <span v-else class="muted">尚无历史版本（重新生成画像后会自动保留旧版）。</span>
    </div>
  </div>
</template>
