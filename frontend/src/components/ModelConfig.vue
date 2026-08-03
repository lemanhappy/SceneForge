<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../lib/api.js'
import ModelSection from './ModelSection.vue'
import VideoProfilesPanel from './VideoProfilesPanel.vue'

const SECTION_INFO = {
  llm: { label: '文本大模型 (LLM)', desc: '写剧本、拆分镜、生成开场钩子；并用于多候选选图与人物一致性评审，所以需支持视觉/多模态（如 qwen-vl-max、gemini-2.5-flash）。纯文本模型会让选图/评审失效。' },
  image: { label: '图像模型', desc: '逐镜生成画面（首帧/尾帧/转场图、角色立绘）。即梦 Seedream 中文文字更好、更少乱码；nano-banana(gemini-image) 多参考图一致性更稳。模型名含 seedream 自动走即梦后端。' },
  video: { label: '视频模型', desc: '由画面帧生成每个镜头的视频片段（图生视频），是最贵的一步。默认豆包 Seedance；画面比例随首帧。' },
  embedding: { label: '向量模型 (Embedding)', desc: '仅「小说改编」流程用于检索召回；常规主题/剧本创作用不到，可不填。' },
  reranker: { label: '重排模型 (Reranker)', desc: '仅「小说改编」流程用于检索结果精排；常规创作用不到，可不填。' },
}

const cfg = ref(null)
const err = ref('')
onMounted(async () => {
  try { cfg.value = await api('GET', '/api/config') } catch (e) { err.value = e.message }
})
</script>

<template>
  <div class="panel">
    <h2>模型配置</h2>
    <div class="muted">每个模型的用途见下方说明。API key 已脱敏；留空则不修改。保存即写入 configs/agent.local.yaml。</div>
    <div v-if="err" class="muted">加载失败：{{ err }}</div>
    <div v-else-if="!cfg" class="muted">加载中…</div>
    <template v-else>
      <template v-for="(fields, sec) in cfg" :key="sec">
        <ModelSection v-if="sec !== 'video' && sec !== 'video_profiles'" :name="sec" :fields="fields"
          :info="SECTION_INFO[sec] || { label: sec, desc: '' }" />
      </template>
      <VideoProfilesPanel v-if="cfg.video_profiles" :initial="cfg.video_profiles" />
    </template>
  </div>
</template>
