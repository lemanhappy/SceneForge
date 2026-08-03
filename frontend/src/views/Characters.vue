<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../lib/api.js'
import { confirmModal } from '../lib/confirm.js'
import CharacterCard from '../components/CharacterCard.vue'

const id = ref('')
const name = ref('')
const desc = ref('')
const alias = ref('')
const msg = ref('')
const chars = ref([])
const listErr = ref('')

async function loadChars() {
  try { const data = await api('GET', '/api/characters'); chars.value = data.characters || [] }
  catch (e) { listErr.value = e.message }
}
onMounted(loadChars)

async function save() {
  const cid = id.value.trim()
  if (!cid) { msg.value = 'asset_id 必填'; return }
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(cid)) { msg.value = 'asset_id 只能用英文字母/数字/-/_，且以字母或数字开头'; return }
  const aliases = alias.value.split(',').map((s) => s.trim()).filter(Boolean)
  const payload = { asset_id: cid, display_name: name.value, visual_prompt: desc.value, description: desc.value, aliases }
  const doSave = async (overwrite) => {
    await api('POST', '/api/characters', overwrite ? { ...payload, overwrite: true } : payload)
    msg.value = overwrite ? '已覆盖更新 ✓' : '已保存 ✓'
    loadChars()
  }
  try { await doSave(false) }
  catch (e) {
    if (e.status === 409) {
      if (await confirmModal('asset_id「' + cid + '」已存在，覆盖更新该角色？（旧画像目录会被沿用）', { okText: '覆盖更新', danger: true })) {
        try { await doSave(true) } catch (e2) { msg.value = '失败：' + e2.message }
      } else { msg.value = '已取消（ID 已存在）' }
    } else { msg.value = '失败：' + e.message }
  }
}
</script>

<template>
  <div>
    <div class="panel">
      <h2>新建/编辑角色模型</h2>
      <div class="grid2">
        <div><label>asset_id（唯一英文ID）</label><input v-model="id" placeholder="wangyunbao" /></div>
        <div><label>display_name</label><input v-model="name" placeholder="王云宝" /></div>
      </div>
      <label>描述 / visual_prompt（用于生成画像）</label>
      <textarea v-model="desc" placeholder="young Chinese man, worn robe, glowing eyes, dark-gold xianxia"></textarea>
      <label>别名（逗号分隔，可选）</label><input v-model="alias" placeholder="云宝,主角" />
      <div style="margin-top:10px"><button class="act" @click="save">保存角色模型</button> <span class="muted">{{ msg }}</span></div>
    </div>
    <div class="panel">
      <h2>角色模型</h2>
      <div v-if="listErr" class="muted">加载失败：{{ listErr }}</div>
      <div v-else-if="!chars.length" class="muted">（暂无角色模型）</div>
      <CharacterCard v-for="c in chars" :key="c.asset_id" :char="c" @reload="loadChars" />
    </div>
  </div>
</template>
