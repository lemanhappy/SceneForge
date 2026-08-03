<script setup>
import { reactive, ref, computed } from 'vue'
import { api } from '../lib/api.js'

const props = defineProps({ name: String, fields: Object, info: Object })
const f = props.fields
const opts = f._options || {}
const catalog = f._model_catalog || {}
const META = new Set(['provider_derived', '_options', '_model_catalog'])
const hasCatalog = Object.keys(catalog).length > 0

// fields in their original order; classify each for rendering
const keys = Object.keys(f).filter((k) => !META.has(k))
function kindOf(k) {
  if (k === 'api_key') return 'apikey'
  if (k === 'model' && hasCatalog) return 'model'
  if (opts[k] && opts[k].length) return 'option'
  return 'text'
}

const apiKey = ref('')
const apiKeyMeta = f.api_key || { set: false, hint: '' }
const text = reactive({})
const optSel = reactive({})
const optCustom = reactive({})
keys.forEach((k) => {
  const kind = kindOf(k)
  if (kind === 'text') text[k] = f[k]
  else if (kind === 'option') {
    if (opts[k].includes(f[k])) { optSel[k] = f[k]; optCustom[k] = '' }
    else { optSel[k] = '__custom__'; optCustom[k] = f[k] }
  }
})

// model picker (厂商 → model cascade)
let curVendor = ''
for (const v of Object.keys(catalog)) if ((catalog[v] || []).includes(f.model)) curVendor = v
const modelCustom = ref(hasCatalog && f.model && !curVendor ? f.model : '')
const selVendor = ref(hasCatalog ? (curVendor || (modelCustom.value ? '__custom__' : Object.keys(catalog)[0] || '')) : '')
const selModel = ref(f.model || '')
const modelOptions = computed(() => catalog[selVendor.value] || [])
function onVendor() {
  if (selVendor.value !== '__custom__' && !modelOptions.value.includes(selModel.value)) selModel.value = modelOptions.value[0] || ''
}

const msg = ref('')
async function save() {
  const payload = {}
  keys.forEach((k) => {
    const kind = kindOf(k)
    if (kind === 'text') payload[k] = text[k]
    else if (kind === 'option') payload[k] = optSel[k] === '__custom__' ? (optCustom[k] || '').trim() : optSel[k]
    else if (kind === 'model') payload.model = selVendor.value === '__custom__' ? (modelCustom.value || '').trim() : selModel.value
  })
  if (keys.includes('api_key') && apiKey.value) payload.api_key = apiKey.value
  try { await api('PUT', '/api/config/' + props.name, payload); msg.value = '已保存 ✓' }
  catch (e) { msg.value = '失败：' + e.message }
}
</script>

<template>
  <div class="char">
    <div class="row" style="justify-content:space-between">
      <b>{{ info.label }}</b>
      <span v-if="f.provider_derived" class="tag">provider: {{ f.provider_derived }}</span>
    </div>
    <div v-if="info.desc" class="muted" style="margin:2px 0 10px;line-height:1.6">{{ info.desc }}</div>

    <template v-for="k in keys" :key="k">
      <!-- api_key -->
      <template v-if="kindOf(k) === 'apikey'">
        <label>api_key
          <span v-if="apiKeyMeta.set" class="tag">已设置 {{ apiKeyMeta.hint }}</span>
          <span v-else class="muted">未设置</span>
        </label>
        <input type="password" v-model="apiKey" placeholder="留空不改" />
      </template>

      <!-- model picker -->
      <template v-else-if="kindOf(k) === 'model'">
        <label>厂商</label>
        <select v-model="selVendor" @change="onVendor">
          <option v-for="v in Object.keys(catalog)" :key="v" :value="v">{{ v }}</option>
          <option value="__custom__">自定义…</option>
        </select>
        <template v-if="selVendor !== '__custom__'">
          <label>model</label>
          <select v-model="selModel"><option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option></select>
        </template>
        <input v-else v-model="modelCustom" style="margin-top:6px" placeholder="自定义 model" />
      </template>

      <!-- option select with 自定义 escape -->
      <template v-else-if="kindOf(k) === 'option'">
        <label>{{ k }}</label>
        <select v-model="optSel[k]">
          <option v-for="o in opts[k]" :key="o" :value="o">{{ o }}</option>
          <option value="__custom__">自定义…</option>
        </select>
        <input v-if="optSel[k] === '__custom__'" v-model="optCustom[k]" style="margin-top:6px" :placeholder="'自定义 ' + k" />
      </template>

      <!-- plain text -->
      <template v-else>
        <label>{{ k }}</label>
        <input v-model="text[k]" />
      </template>
    </template>

    <div style="margin-top:12px"><button class="act" @click="save">保存 {{ name }}</button> <span class="muted">{{ msg }}</span></div>
  </div>
</template>
