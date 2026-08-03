<script setup>
import { computed, ref, reactive, onMounted, onActivated } from 'vue'
import { CircleHelp, Square } from '@lucide/vue'
import { api, watchJob } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import JobProgress from './JobProgress.vue'

const emit = defineEmits(['created', 'sessions-changed'])

const mode = ref('idea')
const idea = ref('')
const title = ref('')
const scriptText = ref('')
const style = ref('')
const req = ref('')
const activeHelp = ref('')

// 本片设置
const lang = ref('zh-CN')
const aspect = ref('landscape')
const qualityTier = ref('balanced')
const continuitySourceId = ref('')
const continuitySources = ref([])
const qualityProfiles = ref([
  { tier: 'economy', label: '省钱', description: '单张关键帧、单个视频候选，速度最快', speed_label: '最快', image_candidates: 1, video_candidates: 1 },
  { tier: 'balanced', label: '均衡', description: '每帧生成 2 张并自动选优，兼顾效果与成本', speed_label: '适中', image_candidates: 2, video_candidates: 1, recommended: true },
  { tier: 'quality', label: '高质量', description: '每帧生成 3 张选优，每镜生成 2 个视频候选', speed_label: '较慢', image_candidates: 3, video_candidates: 2 },
])
const activeQualityProfile = computed(() => qualityProfiles.value.find((item) => item.tier === qualityTier.value) || qualityProfiles.value[1])
const domain = ref('')
const domains = ref([])      // builtin [{key,label}]
const skills = ref([])       // user skills [{key,label}]
const sub = ref(true)
const tts = ref(false)
const voice = ref('')
const bgm = ref('')
const voiceMeta = reactive({ voices: [], provider: '', model: '', voice: '' })
const voices = ref([])       // [{id,label}] for current provider
const bgmTracks = ref([])
const voiceHint = ref('')

// cast / templates
const cast = ref([])
const castSel = ref({})
const propModels = ref([])
const propSel = ref({})
const sceneModels = ref([])
const sceneSel = ref({})
const loraModels = ref([])
const loraSel = ref({})
const templates = ref([])
const tmplIdx = ref('')

const msg = ref('')
const progress = ref(null)   // null = not submitting
const activeJobId = ref('')
const stopping = ref(false)
const audioSrc = ref('')
const audio = ref(null)

function reconcileSelection(selection, models) {
  const valid = new Set(models.map((item) => item.asset_id))
  selection.value = Object.fromEntries(
    Object.entries(selection.value).filter(([assetId, selected]) => selected && valid.has(assetId)),
  )
}

async function loadDomains() {
  try {
    const [d, sk] = await Promise.all([api('GET', '/api/production/domains'), api('GET', '/api/skills').catch(() => ({ skills: [] }))])
    domains.value = d.domains || []
    skills.value = (sk && sk.skills) || []
  } catch (e) { /* ignore */ }
}
async function loadQualityProfiles() {
  try {
    const result = await api('GET', '/api/production/quality-profiles')
    if (Array.isArray(result.profiles) && result.profiles.length) qualityProfiles.value = result.profiles
  } catch (e) { /* use local fallback */ }
}
async function loadAssetModels() {
  const [characters, props, scenes, loras] = await Promise.allSettled([
    api('GET', '/api/characters'),
    api('GET', '/api/assets?asset_type=prop'),
    api('GET', '/api/assets?asset_type=scene'),
    api('GET', '/api/loras'),
  ])
  if (characters.status === 'fulfilled') {
    cast.value = characters.value.characters || []
    reconcileSelection(castSel, cast.value)
  }
  if (props.status === 'fulfilled') {
    propModels.value = props.value.assets || []
    reconcileSelection(propSel, propModels.value)
  }
  if (scenes.status === 'fulfilled') {
    sceneModels.value = scenes.value.assets || []
    reconcileSelection(sceneSel, sceneModels.value)
  }
  if (loras.status === 'fulfilled') {
    loraModels.value = (loras.value.loras || []).filter((item) => item.enabled)
    const valid = new Set(loraModels.value.map((item) => item.lora_id))
    loraSel.value = Object.fromEntries(Object.entries(loraSel.value).filter(([id, selected]) => selected && valid.has(id)))
  }
}
async function loadContinuitySources() {
  try {
    const result = await api('GET', '/api/production')
    continuitySources.value = (result.sessions || []).filter((item) => item.continuity_available)
    if (continuitySourceId.value && !continuitySources.value.some((item) => item.session_id === continuitySourceId.value)) {
      continuitySourceId.value = ''
    }
  } catch (e) { continuitySources.value = [] }
}
function applyContinuitySource() {
  const source = continuitySources.value.find((item) => item.session_id === continuitySourceId.value)
  if (!source) return
  const selectKnown = (selection, models, ids) => {
    const known = new Set(models.value.map((item) => item.asset_id))
    selection.value = {
      ...selection.value,
      ...Object.fromEntries((ids || []).filter((id) => known.has(id)).map((id) => [id, true])),
    }
  }
  selectKnown(castSel, cast, source.character_asset_ids)
  selectKnown(propSel, propModels, source.prop_asset_ids)
  selectKnown(sceneSel, sceneModels, source.scene_asset_ids)
  msg.value = '已沿用上一集的角色、道具和场景资产'
}
async function loadTemplates() {
  try {
    const t = await api('GET', '/api/templates')
    const lu = t.last_used || {}
    if (lu.style && !style.value) style.value = lu.style
    if (lu.user_requirement && !req.value) req.value = lu.user_requirement
    templates.value = t.templates || []
  } catch (e) { /* ignore */ }
}
function autoVoice() {
  if (!lang.value) { voiceHint.value = ''; return }
  const short = lang.value.toLowerCase().startsWith('zh') ? 'zh' : (lang.value.toLowerCase().startsWith('en') ? 'en' : lang.value)
  const vs = voiceMeta.voices || []
  const pick = (vs.find((v) => v.lang === short) || vs.find((v) => v.lang === 'multi') || {}).id || ''
  if (pick && voices.value.some((o) => o.id === pick)) voice.value = pick
  const hasEn = vs.some((v) => v.lang === 'en' || v.lang === 'multi')
  voiceHint.value = (short === 'en' && !hasEn) ? '⚠ 当前音色库没有英文音色（如 MiniMax 仅中文）。英文片建议在「设置→音频」里把 TTS 提供方换成 OpenAI（多语种）。' : ''
}
async function loadDefaults() {
  try { const s = await api('GET', '/api/features'); const f = (s.groups || []).flatMap((g) => g.fields || []).find((x) => x.path === 'subtitle.burn_in'); if (f) sub.value = !!f.value } catch (e) {}
  try {
    const v = await api('GET', '/api/voice')
    tts.value = !!v.enabled
    const c = (v.catalog || {})[v.provider] || { voices: [] }
    Object.assign(voiceMeta, { voices: c.voices || [], provider: v.provider, model: v.model, voice: v.voice })
    voices.value = c.voices || []
    voice.value = v.voice || ''
    autoVoice()
  } catch (e) {}
  try { const b = await api('GET', '/api/bgm'); bgmTracks.value = b.tracks || [] } catch (e) {}
}
onMounted(() => { loadDomains(); loadTemplates(); loadDefaults(); loadContinuitySources(); loadQualityProfiles() })
onActivated(() => { loadAssetModels(); loadContinuitySources() })

function applyTemplate() {
  if (tmplIdx.value === '') return
  const tm = templates.value[parseInt(tmplIdx.value)]; if (!tm) return
  style.value = tm.style || ''; req.value = tm.user_requirement || ''
}
async function saveTemplate() {
  const name = prompt('模板名称：'); if (!name) return
  try { await api('POST', '/api/templates', { name, style: style.value, user_requirement: req.value }); loadTemplates(); msg.value = '模板已保存 ✓' }
  catch (e) { msg.value = '保存模板失败：' + e.message }
}
async function preview() {
  const v = voice.value || voiceMeta.voice
  msg.value = '合成中…'
  try {
    const r = await api('POST', '/api/voice/preview', { provider: voiceMeta.provider, model: voiceMeta.model, voice: v })
    audioSrc.value = 'data:audio/' + (r.format || 'mp3') + ';base64,' + r.audio_b64; msg.value = ''
    setTimeout(() => audio.value && audio.value.play(), 50)
  } catch (e) { msg.value = '试听失败：' + ((e.body && e.body.error) || e.message) }
}

async function submit() {
  if (progress.value) return
  let ideaVal = '', script = ''
  if (mode.value === 'script') { script = scriptText.value.trim(); if (!script) { msg.value = '请粘贴剧本全文'; return } ideaVal = title.value.trim() }
  else { ideaVal = idea.value.trim(); if (!ideaVal) return }
  const character_asset_ids = Object.keys(castSel.value).filter((k) => castSel.value[k])
  const prop_asset_ids = Object.keys(propSel.value).filter((k) => propSel.value[k])
  const scene_asset_ids = Object.keys(sceneSel.value).filter((k) => sceneSel.value[k])
  const lora_ids = Object.keys(loraSel.value).filter((k) => loraSel.value[k])
  msg.value = mode.value === 'script' ? '已提交，导入剧本中…' : '已提交，生成剧本中…'
  progress.value = []
  try {
    api('POST', '/api/templates/remember', { style: style.value, user_requirement: req.value }).catch(() => {})
    const rec = await api('POST', '/api/production/topic', {
      idea: ideaVal, script, mode: mode.value, style: style.value, user_requirement: req.value, domain: domain.value,
      character_asset_ids, prop_asset_ids, scene_asset_ids, lora_ids,
      target_language: lang.value, aspect_ratio: aspect.value,
      quality_tier: qualityTier.value,
      continuity_source_session_id: continuitySourceId.value || undefined,
      tts_enabled: tts.value, subtitle_enabled: sub.value, subtitle_burn_in: sub.value,
      voice: tts.value ? voice.value : '', bgm_track: bgm.value,
    })
    activeJobId.value = rec.job_id || ''
    const job = await watchJob(rec.job_id, (prog) => { progress.value = prog })
    progress.value = null
    activeJobId.value = ''
    stopping.value = false
    if (job.internal_state === 'canceled') {
      msg.value = '剧本生成已终止，已完成内容已保留。'
      emit('sessions-changed')
      return
    }
    if (job.state === 'failed') { msg.value = '失败：' + job.error; return }
    const r = job.result || {}
    msg.value = '✅ 第一阶段（剧本）已生成'
    emit('sessions-changed')
    if (r.session_id) emit('created', r.session_id)
  } catch (e) { progress.value = null; activeJobId.value = ''; stopping.value = false; msg.value = '失败：' + e.message }
}

async function stopCreation() {
  if (!activeJobId.value || stopping.value) return
  if (!await confirmModal('终止剧本生成？已完成的中间结果会保留。若请求已经提交到云端，仍可能产生费用。', { okText: '终止生成', danger: true })) return
  stopping.value = true
  msg.value = '正在终止剧本生成…'
  try {
    await api('POST', '/api/production/jobs/' + activeJobId.value + '/cancel')
  } catch (e) {
    stopping.value = false
    msg.value = '终止失败：' + ((e.body && e.body.error) || e.message)
  }
}
</script>

<template>
  <div>
    <div class="cre-topbar">
      <div class="cre-projectbar">
        <div class="cre-project-copy"><div class="cre-project-title">新建创作</div><div class="cre-project-meta">设置故事、资产和生成规格</div></div>
      </div>
    </div>
    <div class="cre-scroll">
    <div class="panel">
    <div class="row" style="gap:6px;margin-bottom:12px">
      <button class="ghost mode-btn" :class="{ active: mode === 'idea' }" @click="mode = 'idea'">主题生成</button>
      <button class="ghost mode-btn" :class="{ active: mode === 'script' }" @click="mode = 'script'">导入剧本</button>
    </div>

    <div v-if="mode === 'idea'" class="guided-field">
      <div class="field-label-line">
        <label for="creation-idea">主题 / 创意</label>
        <button class="field-help-trigger" type="button" aria-label="查看主题和创意填写说明" aria-describedby="creation-idea-tip"
          :aria-expanded="activeHelp === 'idea'" @mouseenter="activeHelp = 'idea'" @mouseleave="activeHelp = ''"
          @focus="activeHelp = 'idea'" @blur="activeHelp = ''" @click="activeHelp = 'idea'"><CircleHelp :size="15" /></button>
        <span id="creation-idea-tip" class="field-tooltip" :class="{ visible: activeHelp === 'idea' }" role="tooltip">
          <strong>填写说明</strong><span>写清主角、发生了什么、主要冲突和结局方向；一到三句话即可，不必写成完整剧本。</span>
          <strong>参考示例</strong><span>雨夜，一名连续值班的急救员在空荡车站发现母亲留下的饭盒。他原本不愿回家，听见母亲的语音后终于改变决定。</span>
        </span>
      </div>
      <textarea id="creation-idea" v-model="idea" aria-describedby="creation-idea-tip" @focus="activeHelp = ''" @click="activeHelp = ''"
        placeholder="用 1-3 句话描述故事核心"></textarea>
    </div>
    <div v-else class="script-import-fields">
      <div class="guided-field">
        <div class="field-label-line">
          <label for="creation-title">标题（可选）</label>
          <button class="field-help-trigger" type="button" aria-label="查看标题填写说明" aria-describedby="creation-title-tip"
            :aria-expanded="activeHelp === 'title'" @mouseenter="activeHelp = 'title'" @mouseleave="activeHelp = ''"
            @focus="activeHelp = 'title'" @blur="activeHelp = ''" @click="activeHelp = 'title'"><CircleHelp :size="15" /></button>
          <span id="creation-title-tip" class="field-tooltip" :class="{ visible: activeHelp === 'title' }" role="tooltip">
            <strong>填写说明</strong><span>用于历史创作列表和项目识别，不会改写剧本内容；没有确定标题时可以留空。</span>
            <strong>参考示例</strong><span>《雨夜的钥匙》第 1 集：回家</span>
          </span>
        </div>
        <input id="creation-title" v-model="title" aria-describedby="creation-title-tip" placeholder="填写作品或单集标题"
          @focus="activeHelp = ''" @click="activeHelp = ''" />
      </div>
      <div class="guided-field">
        <div class="field-label-line">
          <label for="creation-script">剧本全文</label>
          <button class="field-help-trigger" type="button" aria-label="查看剧本全文填写说明" aria-describedby="creation-script-tip"
            :aria-expanded="activeHelp === 'script'" @mouseenter="activeHelp = 'script'" @mouseleave="activeHelp = ''"
            @focus="activeHelp = 'script'" @blur="activeHelp = ''" @click="activeHelp = 'script'"><CircleHelp :size="15" /></button>
          <span id="creation-script-tip" class="field-tooltip" :class="{ visible: activeHelp === 'script' }" role="tooltip">
            <strong>填写说明</strong><span>粘贴完整剧本后直接进入分镜，不改写原文。建议用“场景一 / 第 1 场 / 内景 / INT.”等标记分场；没有标记时整篇按一个场景处理。</span>
            <strong>参考示例</strong><span>场景一　咖啡馆，午后<br>（王云宝坐在窗边）<br>王云宝：三年了，我终于回来了。</span>
          </span>
        </div>
        <textarea id="creation-script" v-model="scriptText" style="min-height:200px" aria-describedby="creation-script-tip"
          placeholder="粘贴完整剧本内容" @focus="activeHelp = ''" @click="activeHelp = ''"></textarea>
      </div>
    </div>

    <div class="grid2 guided-grid">
      <div class="guided-field">
        <div class="field-label-line">
          <label for="creation-style">风格</label>
          <button class="field-help-trigger" type="button" aria-label="查看风格填写说明" aria-describedby="creation-style-tip"
            :aria-expanded="activeHelp === 'style'" @mouseenter="activeHelp = 'style'" @mouseleave="activeHelp = ''"
            @focus="activeHelp = 'style'" @blur="activeHelp = ''" @click="activeHelp = 'style'"><CircleHelp :size="15" /></button>
          <span id="creation-style-tip" class="field-tooltip" :class="{ visible: activeHelp === 'style' }" role="tooltip">
            <strong>填写说明</strong><span>描述画面类型、时代或地域、色彩光线和表演质感，避免只写“高级感”“好看”。</span>
            <strong>参考示例</strong><span>电影感现实主义，1990 年代南方小城，冷蓝雨夜与暖黄灯光对比，自然肤色，浅景深，表演克制。</span>
          </span>
        </div>
        <input id="creation-style" v-model="style" aria-describedby="creation-style-tip" placeholder="描述视觉基调、色彩光线和表演质感"
          @focus="activeHelp = ''" @click="activeHelp = ''" />
      </div>
      <div class="guided-field">
        <div class="field-label-line">
          <label for="creation-requirements">额外要求</label>
          <button class="field-help-trigger" type="button" aria-label="查看额外要求填写说明" aria-describedby="creation-requirements-tip"
            :aria-expanded="activeHelp === 'requirements'" @mouseenter="activeHelp = 'requirements'" @mouseleave="activeHelp = ''"
            @focus="activeHelp = 'requirements'" @blur="activeHelp = ''" @click="activeHelp = 'requirements'"><CircleHelp :size="15" /></button>
          <span id="creation-requirements-tip" class="field-tooltip" :class="{ visible: activeHelp === 'requirements' }" role="tooltip">
            <strong>填写说明</strong><span>填写必须遵守的时长、镜头数、场景数、叙事节奏、一致性要求和禁止内容，尽量使用明确数字。</span>
            <strong>参考示例</strong><span>竖屏 9:16，总时长 15 秒，严格 3 个镜头、1 个场景；前 3 秒出现钩子；不要新增人物、字幕或水印。</span>
          </span>
        </div>
        <textarea id="creation-requirements" v-model="req" class="compact-textarea"
          aria-describedby="creation-requirements-tip" placeholder="填写时长、镜头数、一致性和禁止项"
          @focus="activeHelp = ''" @click="activeHelp = ''"></textarea>
      </div>
    </div>

    <div class="setbox">
      <div class="setbox-h">本片设置 <span class="muted" style="font-weight:400">（随这条视频，不影响全局）</span></div>
      <div class="pgrid">
        <div class="pfield"><label>语言</label>
          <select v-model="lang" @change="autoVoice"><option value="zh-CN">中文</option><option value="en">英文 English</option><option value="">跟随剧本</option></select>
        </div>
        <div class="pfield"><label>画面比例</label>
          <select v-model="aspect"><option value="landscape">横屏 16:9</option><option value="portrait">竖屏 9:16（抖音/快手）</option><option value="square">方形 1:1</option></select>
        </div>
        <div class="pfield"><label>质量档位</label>
          <div class="quality-seg" role="group" aria-label="质量档位">
            <button v-for="item in qualityProfiles" :key="item.tier" type="button"
              :class="{ on: qualityTier === item.tier }" @click="qualityTier = item.tier">{{ item.label }}</button>
          </div>
          <div v-if="activeQualityProfile" class="quality-profile-note" aria-live="polite">
            <div><strong>{{ activeQualityProfile.description }}</strong><span v-if="activeQualityProfile.recommended" class="quality-recommended">推荐</span></div>
            <span>预计速度：{{ activeQualityProfile.speed_label }} · 关键帧候选 ×{{ activeQualityProfile.image_candidates }} · 视频候选 ×{{ activeQualityProfile.video_candidates }}</span>
          </div>
        </div>
        <div class="pfield"><label>领域 / 风格 Skill</label>
          <select v-model="domain" title="为编剧/分镜/钩子/画面注入题材化推理；含你上传的风格 Skill（在左侧「Skill 市场」上传管理）">
            <option v-for="d in domains" :key="d.key" :value="d.key">{{ d.label }}</option>
            <optgroup v-if="skills.length" label="我的 Skill">
              <option v-for="sk in skills" :key="sk.key" :value="sk.key">{{ sk.label }}</option>
            </optgroup>
          </select>
        </div>
        <div class="pfield"><label for="continuity-source">延续项目</label>
          <select id="continuity-source" v-model="continuitySourceId" @change="applyContinuitySource">
            <option value="">不继承上一集</option>
            <option v-for="item in continuitySources" :key="item.session_id" :value="item.session_id">
              {{ item.idea || item.session_id }}
            </option>
          </select>
        </div>
      </div>
      <div class="chkrow">
        <label class="chk"><input type="checkbox" v-model="sub" /> 烧录字幕</label>
        <label class="chk"><input type="checkbox" v-model="tts" /> 配音（TTS）</label>
      </div>
      <div class="pgrid" style="margin-top:10px">
        <div class="pfield"><label>配音音色</label>
          <div class="row" style="gap:6px;flex-wrap:nowrap">
            <select v-model="voice" style="flex:1">
              <option value="">默认（{{ voiceMeta.voice || '当前' }}）</option>
              <option v-for="v in voices" :key="v.id" :value="v.id">{{ v.label || v.id }}</option>
            </select>
            <button class="ghost" type="button" style="padding:8px 12px;white-space:nowrap" @click="preview">▶ 试听</button>
          </div>
        </div>
        <div class="pfield"><label>背景音乐</label>
          <select v-model="bgm">
            <option value="">默认（全局设置）</option>
            <option value="__none__">无背景音乐</option>
            <option v-for="t in bgmTracks" :key="t" :value="t">{{ t }}</option>
          </select>
        </div>
      </div>
      <audio v-if="audioSrc" ref="audio" :src="audioSrc" style="display:none"></audio>
      <div class="muted" style="margin-top:8px">{{ voiceHint }}</div>
    </div>

    <div class="asset-pickers">
      <div class="asset-pick-row">
        <label>角色模型</label>
        <div class="row asset-chips">
          <span v-if="!cast.length" class="muted">暂无角色模型</span>
          <span v-for="c in cast" :key="c.asset_id" class="chip" :class="{ on: castSel[c.asset_id] }">
            <input type="checkbox" :id="'cast_' + c.asset_id" v-model="castSel[c.asset_id]" />
            <label :for="'cast_' + c.asset_id">{{ c.display_name || c.asset_id }}</label>
          </span>
        </div>
      </div>
      <div class="asset-pick-row">
        <label>道具模型</label>
        <div class="row asset-chips">
          <span v-if="!propModels.length" class="muted">暂无道具模型</span>
          <span v-for="item in propModels" :key="item.asset_id" class="chip" :class="{ on: propSel[item.asset_id] }">
            <input type="checkbox" :id="'prop_' + item.asset_id" v-model="propSel[item.asset_id]" />
            <label :for="'prop_' + item.asset_id">{{ item.display_name }}</label>
          </span>
        </div>
      </div>
      <div class="asset-pick-row">
        <label>场景模型</label>
        <div class="row asset-chips">
          <span v-if="!sceneModels.length" class="muted">暂无场景模型</span>
          <span v-for="item in sceneModels" :key="item.asset_id" class="chip" :class="{ on: sceneSel[item.asset_id] }">
            <input type="checkbox" :id="'scene_' + item.asset_id" v-model="sceneSel[item.asset_id]" />
            <label :for="'scene_' + item.asset_id">{{ item.display_name }}</label>
          </span>
        </div>
      </div>
      <div class="asset-pick-row">
        <label>LoRA 模型（可选）</label>
        <div class="row asset-chips">
          <span v-if="!loraModels.length" class="muted">暂无可用 LoRA，可在 Skill 市场中添加</span>
          <span v-for="item in loraModels" :key="item.lora_id" class="chip" :class="{ on: loraSel[item.lora_id] }"
            :title="item.application_mode === 'native' ? '需要当前图像提供商支持原生 LoRA' : '兼容模式：仅把触发词加入图像提示词'">
            <input type="checkbox" :id="'lora_' + item.lora_id" v-model="loraSel[item.lora_id]" />
            <label :for="'lora_' + item.lora_id">{{ item.display_name }}</label>
            <small>{{ item.application_mode === 'native' ? '原生' : '触发词' }}</small>
          </span>
        </div>
      </div>
    </div>

    <div class="row" style="margin-top:10px">
      <label style="margin:0">模板</label>
      <select v-model="tmplIdx" style="max-width:220px">
        <option value="">（选择模板…）</option>
        <option v-for="(tm, i) in templates" :key="i" :value="String(i)">{{ tm.name }}</option>
      </select>
      <button class="ghost" @click="applyTemplate">应用</button>
      <button class="ghost" @click="saveTemplate">存为模板</button>
    </div>

    <div style="margin-top:10px">
      <button class="act" :disabled="!!progress" @click="submit">{{ mode === 'script' ? '开始（导入剧本）' : '开始（生成剧本）' }}</button>
      <button v-if="progress" class="ghost danger-text" :disabled="stopping" @click="stopCreation"><Square :size="14" />{{ stopping ? '终止中…' : '终止剧本生成' }}</button>
      <span class="muted">{{ msg }}</span>
    </div>
    <JobProgress v-if="progress" :progress="progress" />
    </div>
    </div>
  </div>
</template>
