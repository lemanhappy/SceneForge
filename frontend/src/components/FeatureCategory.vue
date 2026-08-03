<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../lib/api.js'

const props = defineProps({ category: { type: String, required: true } })

const groups = ref([])
const values = ref({})
const msg = ref('')
const loading = ref(true)
const err = ref('')

onMounted(async () => {
  let s
  try { s = await api('GET', '/api/features') } catch (e) { err.value = e.message; loading.value = false; return }
  groups.value = (s.groups || []).filter((g) => (g.category || '') === props.category)
  const v = {}
  groups.value.forEach((g) => (g.fields || []).forEach((f) => { v[f.path] = f.value }))
  values.value = v
  loading.value = false
})

async function save() {
  try { await api('PUT', '/api/features', values.value); msg.value = '已保存 ✓' }
  catch (e) { msg.value = '失败：' + e.message }
}
</script>

<template>
  <div class="panel">
    <h2>{{ category }}</h2>
    <div class="muted">保存即写入创作配置（configs）。</div>
    <div class="cat-panel">
      <div v-if="loading" class="muted">加载中…</div>
      <div v-else-if="err" class="muted">加载失败：{{ err }}</div>
      <div v-else-if="!groups.length" class="muted">该分类暂无可配置项。</div>
      <template v-else>
        <div v-for="g in groups" :key="g.title" class="char">
          <div style="font-weight:600;margin-bottom:4px">{{ g.title }}</div>
          <div v-for="f in g.fields" :key="f.path" class="fwrap">
            <!-- bool -->
            <label v-if="f.type === 'bool'">
              <input type="checkbox" v-model="values[f.path]" /> {{ f.label }}
              <span v-if="f.hint" class="help" :data-tip="f.hint">?</span>
            </label>
            <!-- select -->
            <template v-else-if="f.type === 'select'">
              <label>{{ f.label }}<span v-if="f.hint" class="help" :data-tip="f.hint">?</span></label>
              <select v-model="values[f.path]">
                <option v-for="o in (f.options || [])" :key="o" :value="o">{{ o }}</option>
              </select>
            </template>
            <!-- bounded number -> slider -->
            <template v-else-if="f.type === 'number' && f.min != null && f.max != null">
              <label>{{ f.label }}<span v-if="f.unit">（{{ f.unit }}）</span><span v-if="f.hint" class="help" :data-tip="f.hint">?</span></label>
              <div class="row" style="gap:8px;align-items:center">
                <span v-if="f.min_label" class="muted" style="font-size:12px">{{ f.min_label }}</span>
                <input type="range" v-model.number="values[f.path]" :min="f.min" :max="f.max" :step="f.step || 1" style="flex:1" />
                <span v-if="f.max_label" class="muted" style="font-size:12px">{{ f.max_label }}</span>
                <b style="min-width:34px;text-align:right">{{ values[f.path] }}</b>
              </div>
            </template>
            <!-- plain number -->
            <template v-else-if="f.type === 'number'">
              <label>{{ f.label }}<span v-if="f.unit">（{{ f.unit }}）</span><span v-if="f.hint" class="help" :data-tip="f.hint">?</span></label>
              <input type="number" step="any" v-model="values[f.path]" />
            </template>
            <!-- text / list -->
            <template v-else>
              <label>{{ f.label }}<span v-if="f.hint" class="help" :data-tip="f.hint">?</span></label>
              <input type="text" v-model="values[f.path]" />
            </template>
          </div>
        </div>
        <div style="margin-top:10px"><button class="act" @click="save">保存设置</button> <span class="muted">{{ msg }}</span></div>
      </template>
    </div>
  </div>
</template>
