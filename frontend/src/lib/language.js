const AUDIO_TAGS_ZH = {
  '[Sound Effect]': '[音效]',
  '[Speaker]': '[角色]',
  '[Narrator]': '[旁白]',
  '[Inner Monologue]': '[内心独白]',
}

export function localizeAudioTags(value) {
  let text = String(value || '')
  for (const [source, target] of Object.entries(AUDIO_TAGS_ZH)) text = text.replaceAll(source, target)
  return text
}

export function normalizeAudioTags(value) {
  let text = String(value || '')
  for (const [source, target] of Object.entries(AUDIO_TAGS_ZH)) text = text.replaceAll(target, source)
  return text
}

export function isChineseDominant(value) {
  const text = String(value || '')
    .replace(/\[(?:Sound Effect|Speaker|Narrator|Inner Monologue)\]/gi, '')
    .replace(/<[^>]+>/g, '')
  const hanCount = (text.match(/[\u3400-\u9fff]/g) || []).length
  const latinCount = (text.match(/[A-Za-z]/g) || []).length
  if (latinCount === 0) return true
  return hanCount >= 2 && hanCount >= latinCount
}

export function reviewableChineseText(value, fallback = '旧版英文内容待转换为中文') {
  const text = String(value || '').trim()
  return !text || isChineseDominant(text) ? text : fallback
}

export function reviewableAudioPrompt(value) {
  const text = localizeAudioTags(value).trim()
  if (!text || isChineseDominant(text)) return text
  const dialogue = [...text.matchAll(/["“]([^"”]*[\u3400-\u9fff][^"”]*)["”]/g)]
    .map((match) => match[1].trim())
    .filter(Boolean)
  if (dialogue.length) {
    return dialogue.map((line) => `台词：${line}`).join('\n') + '\n其他声音描述待转换为中文'
  }
  return '旧版声音描述待转换为中文'
}

export function nonChineseStoryboardFields(shot) {
  const fields = []
  const check = (label, value) => {
    const text = String(value || '').trim()
    if (text && !isChineseDominant(text)) fields.push(label)
  }
  check('导演稿', shot && shot.director_desc)
  check('画面提示词', shot && shot.visual_desc)
  check('台词与声音', shot && shot.audio_desc)
  check('画面风格', shot && (shot.visual_style_text || (shot.visual_style || []).join('；')))
  check('避免项', shot && (shot.avoid_text || (shot.avoid || []).join('；')))
  for (const [index, beat] of ((shot && shot.beats) || []).entries()) {
    check(`节拍 ${index + 1} 的镜头运动`, beat.camera)
    check(`节拍 ${index + 1} 的可见动作`, beat.action)
    check(`节拍 ${index + 1} 的细腻表演`, beat.performance)
  }
  return fields
}

export function reviewableVisualPrompt(visualDesc, directorDesc = '') {
  const visual = String(visualDesc || '').trim()
  const director = String(directorDesc || '').trim()
  if (!visual) return director && isChineseDominant(director) ? director : ''
  if (isChineseDominant(visual)) return visual
  return director && isChineseDominant(director) ? director : ''
}
