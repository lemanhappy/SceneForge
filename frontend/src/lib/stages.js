// Staged-pipeline state helpers (ported from the legacy webui). Pure functions:
// reduce a session snapshot to UI state, the stepper index, labels, and which
// action buttons to show.

export const GATE_CN = { script: '剧本', storyboard: '分镜脚本', shot_video: '分镜视频', final: '终审' }

export const STEPS = [
  { gate: 'script', label: '剧本创作' }, { gate: 'storyboard', label: '分镜设计' },
  { gate: 'shot_video', label: '镜头生成' }, { gate: 'final', label: '成片制作' },
]

export const PHASE_CN = { generating: '生成中', review: '待审核', revising: '重做中', done: '已完成', error: '出错', pending: '待开始', interrupted: '已中断' }

export const STATE_HINT = {
  busy: '生成中…（完成后自动刷新）', done: '制作已完成', error: '出错，可重试或提出修改', idle: '准备中',
  interrupted: '⚠ 生成被中断（服务重启或异常）。点「继续生成」从断点续跑——已完成的镜头不会重做。',
}

export function stripStage(stage) {
  stage = stage || ''
  for (const suf of ['_review_pending', '_revision_requested', '_generating']) if (stage.endsWith(suf)) return stage.slice(0, -suf.length)
  return stage
}

export function stageInfo(s) {
  s = s || {}
  const st = s.stage || ''
  if (st === 'completed' || st === 'published') return { idx: 3, phase: 'done', gate: 'final' }
  let phase, gate
  if (/_generating$/.test(st)) { phase = s.interrupted ? 'interrupted' : 'generating'; gate = stripStage(st) }
  else if (/_revision_requested$/.test(st)) { phase = s.interrupted ? 'interrupted' : 'revising'; gate = stripStage(st) }
  else if (/_review_pending$/.test(st)) { phase = 'review'; gate = stripStage(st) }
  else if (st === 'error' || /error|fail/i.test(st)) { phase = 'error'; gate = stripStage(st) }
  else { gate = (s.pending_review && s.pending_review.stage) || stripStage(st); phase = s.pending_review ? 'review' : 'pending' }
  let idx = STEPS.findIndex((x) => x.gate === gate); if (idx < 0) idx = 0
  return { idx, phase, gate }
}

export function stageLabel(s) {
  const info = stageInfo(s)
  return (info.idx + 1) + '/' + STEPS.length + ' ' + STEPS[info.idx].label + ' · ' + (PHASE_CN[info.phase] || info.phase)
}

export function detailState(s) {
  s = s || {}
  if (s.interrupted) return 'interrupted'
  if (s.busy) return 'busy'
  const g = s.pending_review && s.pending_review.stage
  if (g === 'final') return 'final_review'
  if (g) return 'review'
  if (s.stage === 'completed' || s.stage === 'published') return 'done'
  if (s.stage === 'error' || /error|fail/i.test(s.stage || '')) return 'error'
  return 'idle'
}

export function detailButtons(state, s) {
  const hasFinal = !!(s && s.has_final)
  const B = { ok: null, rev: false, view: false, pub: false, cost: true, clean: false, cont: false, reopen: false }
  if (state === 'busy') return { ...B, ok: null, rev: false, view: hasFinal, cost: false }
  if (state === 'interrupted') return { ...B, cont: true, rev: true, view: hasFinal }
  if (state === 'review') {
    const gate = s && s.pending_review && s.pending_review.stage
    const nextAction = {
      script: '进入分镜设计',
      storyboard: '进入镜头生成',
      shot_video: '进入成片制作',
    }[gate] || '进入下一阶段'
    return { ...B, ok: nextAction, rev: true, view: hasFinal }
  }
  if (state === 'final_review') return { ...B, ok: '完成制作', rev: true, view: true }
  if (state === 'done') return { ...B, ok: null, rev: false, view: true, pub: true, clean: true, reopen: true }
  if (state === 'error') return { ...B, ok: '重试', rev: true, view: hasFinal }
  return { ...B, ok: null, rev: false }
}

// per-shot quality badge text from the consistency critic's verdict
const QDIM_CN = { score: '人物一致性', aesthetic: '画面质量', adherence: '镜头贴合', temporal: '镜内连贯' }
export function qualityBadge(q) {
  if (!q || typeof q.ok === 'undefined') return null
  if (q.ok === false) {
    const fails = (q.failed || []).map((k) => QDIM_CN[k] || k).join('、') || '质量'
    return { ok: false, cls: 'q-bad', text: '⚠ ' + fails, title: '未达标：' + fails + '，建议「重生成本镜」' }
  }
  const warnings = []
  for (const signal of [q.deterministic, q.identity_signal, q.prop_signal]) {
    for (const issue of (signal && signal.issues) || []) {
      warnings.push(issue.message || issue.code || String(issue))
    }
  }
  if (warnings.length) {
    return { ok: true, cls: 'q-warn', text: '本地检测需复核', title: warnings.join('\n') }
  }
  const sc = (typeof q.score === 'number') ? (' ' + q.score.toFixed(2)) : ''
  return { ok: true, cls: 'q-ok', text: '✓ 质量' + sc, title: '质量校验通过' }
}
