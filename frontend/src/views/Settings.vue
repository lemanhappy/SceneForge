<script setup>
import { ref } from 'vue'
import { Captions, Clapperboard, KeyRound, Plug, Settings2, ShieldCheck, Volume2 } from '@lucide/vue'
import ModelConfig from '../components/ModelConfig.vue'
import GeneralSettings from '../components/GeneralSettings.vue'
import VoicePanel from '../components/VoicePanel.vue'
import BgmPanel from '../components/BgmPanel.vue'
import SfxPanel from '../components/SfxPanel.vue'
import FeatureCategory from '../components/FeatureCategory.vue'

const SECTIONS = [
  { key: 'general', label: '常规', icon: Settings2 },
  { key: 'model', label: '模型', icon: KeyRound },
  { key: 'audio', label: '音频', icon: Volume2 },
  { key: '画面', label: '画面', icon: Clapperboard },
  { key: '文案与字幕', label: '字幕与文案', icon: Captions },
  { key: '合规与质量', label: '合规与质量', icon: ShieldCheck },
  { key: '扩展', label: '扩展', icon: Plug },
]
const active = ref('general')
</script>

<template>
  <div>
    <div class="subnav">
      <button v-for="sec in SECTIONS" :key="sec.key" class="subtab" :class="{ active: active === sec.key }" @click="active = sec.key">
        <component :is="sec.icon" :size="15" />{{ sec.label }}
      </button>
    </div>
    <div>
      <GeneralSettings v-if="active === 'general'" />
      <ModelConfig v-else-if="active === 'model'" :key="'model'" />
      <template v-else-if="active === 'audio'">
        <div class="panel" style="background:var(--acc-soft)">
          <div class="muted">这里是<b>全局默认与接入</b>（提供方/Key/音色库/曲库/音量）。<b>是否配音、配音音色、是否字幕、背景音乐曲目是「每条视频」的选项，请在「创作」页按视频选择</b>；下面设的值作为新视频的默认。</div>
        </div>
        <div class="panel"><h2>配音音色（提供方/默认音色）</h2><div class="muted">选择 TTS 提供方与默认音色，可试听。保存即写入创作配置（key 仍读模型配置）。</div><VoicePanel /></div>
        <div class="panel"><h2>背景音乐（曲库/默认）</h2><div class="muted">上传/管理曲库 + 默认曲目 + 音量。曲目可在创作页按视频覆盖。</div><BgmPanel /></div>
        <div class="panel"><h2>音效库</h2><div class="muted">上传音效文件（按关键词命名，如 thunder_clap.mp3），分镜 [Sound Effect] 描述按词匹配。</div><SfxPanel /></div>
      </template>
      <FeatureCategory v-else :key="active" :category="active" />
    </div>
  </div>
</template>
