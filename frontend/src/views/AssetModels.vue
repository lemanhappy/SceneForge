<script setup>
import { ref, watch } from 'vue'
import { api, mediaUrl } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import { openLightbox } from '../lib/lightbox.js'
import Characters from './Characters.vue'

const tab = ref('character')
const items = ref([])
const listMsg = ref('')
const formMsg = ref('')
const revision = ref(Date.now())
const editingId = ref('')
const assetId = ref('')
const displayName = ref('')
const visualPrompt = ref('')
const negativePrompt = ref('')
const consistencyNotes = ref('')
const tags = ref('')
const sceneBible = ref({
  spatial_layout: '', fixed_objects: '', lighting: '', time_of_day: '', weather: '',
  color_palette: '', forbidden_changes: '',
})
const propBible = ref({
  appearance: '', materials: '', colors: '', ownership: '', initial_location: '',
  condition: '', forbidden_changes: '',
})

const labels = {
  character: '角色模型',
  prop: '道具模型',
  scene: '场景模型',
}

async function load() {
  if (tab.value === 'character') return
  try {
    const result = await api('GET', '/api/assets?asset_type=' + tab.value)
    items.value = result.assets || []
    listMsg.value = ''
  } catch (e) { listMsg.value = '加载失败：' + e.message }
}

function clearForm() {
  editingId.value = ''
  assetId.value = ''
  displayName.value = ''
  visualPrompt.value = ''
  negativePrompt.value = ''
  consistencyNotes.value = ''
  tags.value = ''
  sceneBible.value = {
    spatial_layout: '', fixed_objects: '', lighting: '', time_of_day: '', weather: '',
    color_palette: '', forbidden_changes: '',
  }
  propBible.value = {
    appearance: '', materials: '', colors: '', ownership: '', initial_location: '',
    condition: '', forbidden_changes: '',
  }
  formMsg.value = ''
}

watch(tab, async () => {
  clearForm()
  items.value = []
  listMsg.value = ''
  if (tab.value !== 'character') await load()
}, { immediate: true })

const split = (value) => String(value || '').split(/[,，]/).map((item) => item.trim()).filter(Boolean)
async function save() {
  const id = assetId.value.trim()
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(id)) {
    formMsg.value = '唯一 ID 只能使用英文字母、数字、- 和 _'
    return
  }
  const payload = {
    asset_id: id,
    asset_type: tab.value,
    display_name: displayName.value.trim() || id,
    visual_prompt: visualPrompt.value.trim(),
    description: visualPrompt.value.trim(),
    negative_prompt: negativePrompt.value.trim(),
    consistency_notes: consistencyNotes.value.trim(),
    tags: split(tags.value),
    scene_bible: tab.value === 'scene' ? {
      spatial_layout: sceneBible.value.spatial_layout,
      fixed_objects: split(sceneBible.value.fixed_objects),
      lighting: sceneBible.value.lighting,
      time_of_day: sceneBible.value.time_of_day,
      weather: sceneBible.value.weather,
      color_palette: split(sceneBible.value.color_palette),
      forbidden_changes: split(sceneBible.value.forbidden_changes),
    } : null,
    prop_bible: tab.value === 'prop' ? {
      appearance: propBible.value.appearance,
      materials: split(propBible.value.materials),
      colors: split(propBible.value.colors),
      ownership: propBible.value.ownership,
      initial_location: propBible.value.initial_location,
      condition: propBible.value.condition,
      forbidden_changes: split(propBible.value.forbidden_changes),
    } : null,
  }
  const isEditing = editingId.value === id
  const submit = (overwrite) => isEditing
    ? api('PUT', '/api/assets/' + id, payload)
    : api('POST', '/api/assets', overwrite ? { ...payload, overwrite: true } : payload)
  try {
    await submit(false)
    await load()
    clearForm()
    formMsg.value = isEditing ? '已更新' : '已保存'
  } catch (e) {
    if (e.status === 409 && await confirmModal('该 ID 已存在，覆盖更新？', { okText: '覆盖', danger: true })) {
      try { await submit(true); formMsg.value = '已更新'; await load() }
      catch (second) { formMsg.value = '保存失败：' + second.message }
    } else if (e.status !== 409) formMsg.value = '保存失败：' + e.message
  }
}
function edit(item) {
  editingId.value = item.asset_id
  assetId.value = item.asset_id
  displayName.value = item.display_name || ''
  visualPrompt.value = item.visual_prompt || item.description || ''
  negativePrompt.value = item.negative_prompt || ''
  consistencyNotes.value = item.consistency_notes || ''
  tags.value = (item.tags || []).join(', ')
  const scene = item.scene_bible || {}
  sceneBible.value = {
    spatial_layout: scene.spatial_layout || '',
    fixed_objects: (scene.fixed_objects || []).join(', '),
    lighting: scene.lighting || '',
    time_of_day: scene.time_of_day || '',
    weather: scene.weather || '',
    color_palette: (scene.color_palette || []).join(', '),
    forbidden_changes: (scene.forbidden_changes || []).join(', '),
  }
  const prop = item.prop_bible || {}
  propBible.value = {
    appearance: prop.appearance || '',
    materials: (prop.materials || []).join(', '),
    colors: (prop.colors || []).join(', '),
    ownership: prop.ownership || '',
    initial_location: prop.initial_location || '',
    condition: prop.condition || '',
    forbidden_changes: (prop.forbidden_changes || []).join(', '),
  }
  formMsg.value = ''
}
async function generate(item) {
  item._busy = true
  try {
    await api('POST', '/api/assets/' + item.asset_id + '/generate')
    revision.value = Date.now()
    await load()
  } catch (e) { listMsg.value = '生成失败：' + e.message }
  item._busy = false
}
async function remove(item) {
  if (!await confirmModal('删除' + labels[item.asset_type] + '「' + item.display_name + '」？', { okText: '删除', danger: true })) return
  try { await api('DELETE', '/api/assets/' + item.asset_id); await load() }
  catch (e) { listMsg.value = '删除失败：' + e.message }
}
const imageUrl = (item) => mediaUrl('/api/assets/' + item.asset_id + '/image?v=' + revision.value)
</script>

<template>
  <div>
    <div class="subnav asset-tabs" role="tablist" aria-label="资产模型分类">
      <button v-for="kind in ['character', 'prop', 'scene']" :key="kind" class="subtab"
        :class="{ active: tab === kind }" role="tab" @click="tab = kind">{{ labels[kind] }}</button>
    </div>

    <Characters v-if="tab === 'character'" />
    <template v-else>
      <section class="panel asset-editor">
        <h2>{{ editingId ? '编辑' : '新建' }}{{ labels[tab] }}</h2>
        <div class="grid2">
          <div><label>唯一 ID</label><input v-model="assetId" placeholder="hero_sword" :disabled="!!editingId" /></div>
          <div><label>显示名称</label><input v-model="displayName" :placeholder="tab === 'prop' ? '玄铁长剑' : '王府后院'" /></div>
        </div>
        <label>固定外观提示词</label>
        <textarea v-model="visualPrompt" :placeholder="tab === 'prop' ? 'black iron sword, worn leather grip, silver crack on blade' : 'ancient courtyard, grey brick walls, old plum tree, stone path'"></textarea>
        <template v-if="tab === 'scene'">
          <label>空间布局</label><textarea v-model="sceneBible.spatial_layout" placeholder="入口在左，中央长椅，售票窗在右，保持固定空间关系"></textarea>
          <div class="grid2">
            <div><label>固定物件（逗号分隔）</label><input v-model="sceneBible.fixed_objects" /></div>
            <div><label>光照</label><input v-model="sceneBible.lighting" /></div>
            <div><label>时间</label><input v-model="sceneBible.time_of_day" /></div>
            <div><label>天气</label><input v-model="sceneBible.weather" /></div>
            <div><label>色彩基调（逗号分隔）</label><input v-model="sceneBible.color_palette" /></div>
            <div><label>布局禁止变化（逗号分隔）</label><input v-model="sceneBible.forbidden_changes" /></div>
          </div>
        </template>
        <template v-if="tab === 'prop'">
          <label>标准外观</label><textarea v-model="propBible.appearance" placeholder="记录形状、结构、磨损和辨识特征"></textarea>
          <div class="grid2">
            <div><label>材质（逗号分隔）</label><input v-model="propBible.materials" /></div>
            <div><label>颜色（逗号分隔）</label><input v-model="propBible.colors" /></div>
            <div><label>归属</label><input v-model="propBible.ownership" /></div>
            <div><label>初始位置</label><input v-model="propBible.initial_location" /></div>
            <div><label>状态与损坏程度</label><input v-model="propBible.condition" /></div>
            <div><label>禁止变化（逗号分隔）</label><input v-model="propBible.forbidden_changes" /></div>
          </div>
        </template>
        <div class="grid2">
          <div><label>一致性要求</label><input v-model="consistencyNotes" placeholder="材质、颜色、结构保持不变" /></div>
          <div><label>禁止变化</label><input v-model="negativePrompt" placeholder="不要改变颜色，不要增加文字或徽标" /></div>
        </div>
        <label>标签（逗号分隔）</label><input v-model="tags" placeholder="古装, 主道具" />
        <div class="row" style="margin-top:12px">
          <button class="act" type="button" @click="save">{{ editingId ? '更新' : '保存' }}{{ labels[tab] }}</button>
          <button v-if="editingId" class="ghost" type="button" @click="clearForm">取消</button>
          <span class="muted">{{ formMsg }}</span>
        </div>
      </section>

      <section class="panel">
        <h2>{{ labels[tab] }}</h2>
        <div v-if="listMsg" class="errnote">{{ listMsg }}</div>
        <div v-if="!items.length" class="muted asset-empty">暂无{{ labels[tab] }}</div>
        <div v-else class="asset-model-grid">
          <article v-for="item in items" :key="item.asset_id" class="asset-model-card">
            <div class="asset-preview">
              <img v-if="item.assets && item.assets.reference" :src="imageUrl(item)" :alt="item.display_name" @click="openLightbox(imageUrl(item))" />
              <span v-else>暂无参考图</span>
            </div>
            <div class="asset-model-body">
              <div class="asset-model-title">{{ item.display_name }} <small>{{ item.asset_id }}</small></div>
              <div class="asset-model-desc">{{ item.visual_prompt || item.description || '未填写外观提示词' }}</div>
              <div class="asset-tags"><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div>
              <div class="row asset-model-actions">
                <button class="ghost" type="button" @click="edit(item)">编辑</button>
                <button class="ghost" :disabled="item._busy" @click="generate(item)">{{ item._busy ? '生成中…' : '生成参考图' }}</button>
                <button class="ghost danger-text" @click="remove(item)">删除</button>
              </div>
            </div>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>
