<script setup>
import { reactive, ref, onMounted } from 'vue'
import { Boxes, Palette, Pencil, Plus, Trash2 } from '@lucide/vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'

const data = ref({ skills: [], examples: [], builtins: [], markets: [], market_url: '' })
const msg = ref('')
const fileInput = ref(null)
const tab = ref('skills')
const loras = ref([])
const loraMsg = ref('')
const editingLoraId = ref('')
const loraForm = reactive({
  lora_id: '', display_name: '', provider: '', base_model: '', source_type: 'cloud',
  model_ref: '', trigger_words: '', default_weight: 0.8, application_mode: 'native', tags: '', notes: '', enabled: true,
})

const sectionsOf = (s) => [['剧本', s.script], ['分镜', s.storyboard], ['视频', s.video], ['钩子', s.hook]].filter((p) => p[1])
const tags = (s) => sectionsOf(s).map((p) => p[0])

async function refresh() {
  try {
    const [skillsData, loraData] = await Promise.all([
      api('GET', '/api/skills'), api('GET', '/api/loras').catch(() => ({ loras: [] })),
    ])
    data.value = skillsData
    loras.value = loraData.loras || []
  }
  catch (e) { msg.value = '加载失败：' + e.message }
}
onMounted(refresh)

async function upload() {
  const f = fileInput.value?.files?.[0]
  if (!f) { msg.value = '请先选择一个 .md 文件'; return }
  msg.value = '上传中…'
  let text = ''
  try { text = await f.text() } catch (e) { msg.value = '读取文件失败'; return }
  try {
    const r = await api('POST', '/api/skills', { filename: f.name, content: text })
    if (r && r.ok) { msg.value = '✅ 已上传：' + ((r.skill && r.skill.label) || ''); if (fileInput.value) fileInput.value.value = ''; refresh() }
    else msg.value = '上传失败：' + ((r && r.error) || '未知错误')
  } catch (e) { msg.value = '上传失败：' + e.message }
}

async function importExample(key) {
  msg.value = '导入中…'
  try {
    const r = await api('POST', '/api/skills/import', { key })
    msg.value = r && r.ok ? '✅ 已导入：' + ((r.skill && r.skill.label) || '') : '导入失败：' + ((r && r.error) || '未知错误')
  } catch (e) { msg.value = '导入失败：' + e.message }
  refresh()
}

async function fork(key) {
  msg.value = '复制中…'
  try {
    const r = await api('POST', '/api/skills/fork', { key })
    msg.value = r && r.ok ? '✅ 已复制为我的 Skill：' + ((r.skill && r.skill.label) || '') : '复制失败：' + ((r && r.error) || '未知错误')
  } catch (e) { msg.value = '复制失败：' + e.message }
  refresh()
}

async function del(key) {
  if (!await confirmModal('删除这个 Skill？此操作不可撤销。', { okText: '删除', danger: true })) return
  try { await api('DELETE', '/api/skills/' + encodeURIComponent(key)) }
  catch (e) { msg.value = '删除失败：' + e.message }
  refresh()
}

function resetLoraForm() {
  editingLoraId.value = ''
  Object.assign(loraForm, {
    lora_id: '', display_name: '', provider: '', base_model: '', source_type: 'cloud',
    model_ref: '', trigger_words: '', default_weight: 0.8, application_mode: 'native', tags: '', notes: '', enabled: true,
  })
  loraMsg.value = ''
}

function editLora(item) {
  editingLoraId.value = item.lora_id
  Object.assign(loraForm, {
    ...item,
    trigger_words: (item.trigger_words || []).join(', '),
    tags: (item.tags || []).join(', '),
  })
  loraMsg.value = ''
}

async function saveLora() {
  const payload = { ...loraForm }
  if (!payload.lora_id.trim()) { loraMsg.value = '请填写 LoRA ID'; return }
  try {
    const path = editingLoraId.value ? '/api/loras/' + encodeURIComponent(editingLoraId.value) : '/api/loras'
    await api(editingLoraId.value ? 'PUT' : 'POST', path, payload)
    const success = editingLoraId.value ? 'LoRA 已更新' : 'LoRA 已加入资源库'
    await refresh()
    resetLoraForm()
    loraMsg.value = success
  } catch (e) { loraMsg.value = '保存失败：' + ((e.body && e.body.error) || e.message) }
}

async function deleteLora(item) {
  if (!await confirmModal('删除 LoRA「' + item.display_name + '」？已创建的项目仍保留当时的配置快照。', { okText: '删除', danger: true })) return
  try {
    await api('DELETE', '/api/loras/' + encodeURIComponent(item.lora_id))
    if (editingLoraId.value === item.lora_id) resetLoraForm()
    await refresh()
  } catch (e) { loraMsg.value = '删除失败：' + e.message }
}
</script>

<template>
  <div>
    <div class="subnav skill-market-tabs" role="tablist" aria-label="扩展资源类型">
      <button class="subtab" :class="{ active: tab === 'skills' }" type="button" @click="tab = 'skills'"><Palette :size="15" />风格 Skill</button>
      <button class="subtab" :class="{ active: tab === 'loras' }" type="button" @click="tab = 'loras'"><Boxes :size="15" />LoRA 模型</button>
    </div>

    <template v-if="tab === 'skills'">
    <div class="fhint" style="margin-bottom:14px">
      风格 Skill = 一个 .md 文件（剧本 / 分镜 / 视频 / 钩子 任意小节），生成时注入对应环节。
      在「创作」页的「领域 / 风格 Skill」下拉里选用。纯文本提示词，导入安全。
    </div>

    <!-- 市场横幅（置顶醒目） -->
    <div class="sk-mktbar">
      <div class="sk-mktbar-h">🛒 去 Skill 市场找更多风格</div>
      <div class="sk-mktbar-sub">在这些站点浏览 / 下载风格 Skill，下载的 .md 用下方「上传」导入即可</div>
      <div v-if="data.markets.length" class="sk-markets">
        <a v-for="m in data.markets" :key="m.url" class="sk-mkt" target="_blank" rel="noopener" :href="m.url">
          <span class="nm">{{ m.name }} ↗</span><span v-if="m.note" class="nt">{{ m.note }}</span>
        </a>
      </div>
      <div v-else class="sk-empty">（无市场链接）</div>
      <div class="sk-mktbar-note">⚠ 这些是通用 AI Agent 技能市场（SKILL.md 标准，主要面向编程类 Agent），与本工具的风格 Skill 格式不同，
        下载的文件通常<b>不能直接导入</b>，仅作生态 / 灵感参考。想直接可用，请用下方「模板」。可在 设置→扩展 配置你自己的市场地址。</div>
    </div>

    <!-- 可直接选用 -->
    <div class="sk-zone usable">
      <div class="sk-zone-h"><span class="dot ok"></span>可直接选用 <span class="sub">— 已出现在「创作」页的「领域 / 风格 Skill」下拉里</span></div>

      <div class="sk-h">🎨 我的 Skill <span class="cnt">{{ data.skills.length }}</span> <span class="sub">· 你上传 / 导入的</span>
        <span class="row" style="margin-left:auto;gap:8px">
          <input ref="fileInput" type="file" accept=".md,text/markdown,text/plain" style="max-width:190px" />
          <button class="sk-btn add" type="button" @click="upload">⤒ 上传 .md</button>
        </span>
      </div>
      <div class="fhint" style="margin:-2px 0 8px">{{ msg }}</div>
      <div v-if="data.skills.length" class="sk-grid">
        <div v-for="s in data.skills" :key="s.key" class="sk-card">
          <div class="sk-card-top">
            <div>
              <div class="row" style="gap:7px"><span class="sk-name">{{ s.label }}</span><span class="sk-badge ok">可选用</span></div>
              <div v-if="s.author" class="sk-author">{{ s.author }}</div>
            </div>
            <button class="sk-btn del" type="button" @click="del(s.key)">✕ 删除</button>
          </div>
          <div v-if="tags(s).length" class="sk-tags"><span v-for="t in tags(s)" :key="t" class="pill">{{ t }}</span></div>
          <details v-if="sectionsOf(s).length" class="sk-prev"><summary></summary>
            <div v-for="p in sectionsOf(s)" :key="p[0]" class="seg"><b>{{ p[0] }}</b>{{ p[1] }}</div>
          </details>
        </div>
      </div>
      <div v-else class="sk-empty">还没有任何 Skill。用右上角「上传 .md」，或从下方「内置风格 / 模板」复制 / 导入。</div>

      <div class="sk-sec">
        <div class="sk-h">🧩 内置风格 <span class="cnt">{{ data.builtins.length }}</span> <span class="sub">· 程序自带·只读，创作页可直接选用</span></div>
        <div v-if="data.builtins.length" class="sk-grid">
          <div v-for="s in data.builtins" :key="s.key" class="sk-card">
            <div class="sk-card-top">
              <div>
                <div class="row" style="gap:7px"><span class="sk-name">{{ s.label }}</span><span class="sk-badge ok">可选用</span></div>
                <div v-if="s.author" class="sk-author">{{ s.author }}</div>
              </div>
              <button class="sk-btn add" type="button" @click="fork(s.key)">⎘ 复制</button>
            </div>
            <div v-if="tags(s).length" class="sk-tags"><span v-for="t in tags(s)" :key="t" class="pill">{{ t }}</span></div>
            <details v-if="sectionsOf(s).length" class="sk-prev"><summary></summary>
              <div v-for="p in sectionsOf(s)" :key="p[0]" class="seg"><b>{{ p[0] }}</b>{{ p[1] }}</div>
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- 模板（需导入） -->
    <div class="sk-zone tpl">
      <div class="sk-zone-h"><span class="dot gray"></span>模板 <span class="sub">— 需先「导入 / 复制」成你自己的 Skill 才能选用</span></div>
      <div class="sk-sec">
        <div class="sk-h">📦 示例模板 <span class="cnt">{{ data.examples.length }}</span> <span class="sub">· 点「导入」后才可选用</span></div>
        <div v-if="data.examples.length" class="sk-grid">
          <div v-for="s in data.examples" :key="s.key" class="sk-card">
            <div class="sk-card-top">
              <div>
                <div class="row" style="gap:7px"><span class="sk-name">{{ s.label }}</span>
                  <span class="sk-badge" :class="s.installed ? 'ok' : 'gray'">{{ s.installed ? '可选用' : '模板' }}</span>
                </div>
                <div v-if="s.author" class="sk-author">{{ s.author }}</div>
              </div>
              <span v-if="s.installed" class="sk-installed">✓ 已导入</span>
              <button v-else class="sk-btn add" type="button" @click="importExample(s.key)">⤓ 导入</button>
            </div>
            <div v-if="tags(s).length" class="sk-tags"><span v-for="t in tags(s)" :key="t" class="pill">{{ t }}</span></div>
            <details v-if="sectionsOf(s).length" class="sk-prev"><summary></summary>
              <div v-for="p in sectionsOf(s)" :key="p[0]" class="seg"><b>{{ p[0] }}</b>{{ p[1] }}</div>
            </details>
          </div>
        </div>
      </div>
    </div>
    </template>

    <template v-else>
      <div class="lora-intro">
        <div><strong>LoRA 资源库</strong><span>专业用户可登记本地权重或云端模型 ID；普通用户可以不选择，仍使用角色参考图保持一致性。</span></div>
        <span class="lora-count">{{ loras.length }} 个模型</span>
      </div>

      <section class="panel lora-editor">
        <div class="lora-editor-head"><div><h2>{{ editingLoraId ? '编辑 LoRA' : '添加 LoRA' }}</h2><span>保存后可在“单条视频”的 LoRA 模型中选择。</span></div><button v-if="editingLoraId" class="ghost" type="button" @click="resetLoraForm">取消编辑</button></div>
        <div class="grid2">
          <div><label>LoRA ID</label><input v-model="loraForm.lora_id" :disabled="!!editingLoraId" placeholder="wangyunbao_lora" /></div>
          <div><label>显示名称</label><input v-model="loraForm.display_name" placeholder="王云宝角色 LoRA" /></div>
          <div><label>来源</label><select v-model="loraForm.source_type"><option value="cloud">云端模型 ID</option><option value="local">本地模型文件</option></select></div>
          <div><label>服务商</label><input v-model="loraForm.provider" placeholder="comfyui / replicate / fal" /></div>
          <div><label>基础模型</label><input v-model="loraForm.base_model" placeholder="例如 FLUX.1-dev / SDXL" /></div>
          <div><label>{{ loraForm.source_type === 'local' ? '模型绝对路径' : '云端模型 ID' }}</label><input v-model="loraForm.model_ref" :placeholder="loraForm.source_type === 'local' ? 'D:\\models\\character.safetensors' : 'owner/model:version'" /></div>
          <div><label>应用方式</label><select v-model="loraForm.application_mode"><option value="native">原生 LoRA（后端必须支持）</option><option value="trigger">仅使用触发词（兼容模式）</option></select></div>
          <div><label>默认权重</label><input v-model.number="loraForm.default_weight" type="number" min="0" max="2" step="0.05" /></div>
          <div><label>触发词（逗号分隔）</label><input v-model="loraForm.trigger_words" placeholder="wyb_person, black glasses" /></div>
          <div><label>标签（逗号分隔）</label><input v-model="loraForm.tags" placeholder="角色, 写实, 男性" /></div>
        </div>
        <label>备注</label><textarea v-model="loraForm.notes" class="compact-textarea" placeholder="适用画风、建议权重、版本说明"></textarea>
        <label class="toggle-line"><input v-model="loraForm.enabled" type="checkbox" />允许在单条视频中选择</label>
        <div class="row lora-form-actions"><button class="act" type="button" @click="saveLora"><Plus v-if="!editingLoraId" :size="14" />{{ editingLoraId ? '保存修改' : '加入资源库' }}</button><span class="muted">{{ loraMsg }}</span></div>
      </section>

      <section class="sk-zone usable lora-library">
        <div class="sk-zone-h"><span class="dot ok"></span>可选 LoRA <span class="sub">新建项目可按需多选，未选择时不会改变现有生成流程</span></div>
        <div v-if="!loras.length" class="sk-empty">暂无 LoRA。专业用户可在上方登记本地权重或云端模型 ID。</div>
        <div v-else class="sk-grid">
          <article v-for="item in loras" :key="item.lora_id" class="sk-card lora-card">
            <div class="sk-card-top">
              <div><div class="row" style="gap:7px"><span class="sk-name">{{ item.display_name }}</span><span class="sk-badge" :class="item.enabled ? 'ok' : 'gray'">{{ item.enabled ? '可选择' : '已停用' }}</span></div><div class="sk-author">{{ item.lora_id }}</div></div>
              <div class="lora-card-actions"><button class="iconbtn" type="button" title="编辑 LoRA" @click="editLora(item)"><Pencil :size="14" /></button><button class="iconbtn danger-text" type="button" title="删除 LoRA" @click="deleteLora(item)"><Trash2 :size="14" /></button></div>
            </div>
            <div class="lora-meta"><span>{{ item.application_mode === 'native' ? '原生 LoRA' : '仅触发词' }}</span><span>{{ item.source_type === 'local' ? '本地' : '云端' }}</span><span v-if="item.base_model">{{ item.base_model }}</span><span>权重 {{ item.default_weight }}</span></div>
            <div v-if="item.trigger_words && item.trigger_words.length" class="sk-tags"><span v-for="word in item.trigger_words" :key="word" class="pill">{{ word }}</span></div>
            <div class="lora-model-ref" :title="item.model_ref">{{ item.model_ref || '未配置模型位置' }}</div>
            <p v-if="item.notes">{{ item.notes }}</p>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>
