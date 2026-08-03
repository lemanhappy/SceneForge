// Derive a human progress label + fraction + per-shot status grid from a job's
// event log (ported from the legacy webui).

export const STAGE_CN = {
  render_start: '开始渲染', develop_story: '构思剧情', extract_characters: '提取角色',
  character_portraits_start: '生成角色立绘', character_portraits_done: '角色立绘就绪',
  load_storyboard: '设计分镜', storyboard_ready: '分镜就绪', load_shot_descriptions: '拆解分镜脚本',
  shot_descriptions_ready: '分镜脚本就绪', prompt_preflight: '检查提示词与连续性', load_camera_tree: '规划镜头', camera_tree_ready: '镜头规划就绪',
  frames_start: '生成关键帧', video_clips_start: '生成视频片段', concat_start: '拼接成片',
  final_video_exists: '拼接成片', audio_post_start: '处理音频', subtitle_burn_start: '烧录字幕',
  poster_exported: '导出封面', consistency_fix: '质量校验·重生成', consistency_ok: '质量校验通过',
  render_done: '渲染完成', hook_generated: '生成开场钩子',
  timeline_render_start: '重新合成剪辑成片', timeline_render_done: '剪辑成片已就绪',
  timeline_reset_start: '恢复原始成片', timeline_reset_done: '原始成片已恢复',
}

export function jobProgress(prog) {
  prog = prog || []
  if (!prog.length) return { label: '处理中…（可能调用模型，较慢）', frac: null, steps: 0 }
  let shotCount = 0; const framesDone = new Set(), clipsDone = new Set()
  let lastStage = '', lastMsg = ''
  let steps = 0
  for (const e of prog) {
    const m = e.meta || {}; lastStage = e.stage || lastStage; lastMsg = e.message || lastMsg
    if (m.shot_count) shotCount = Math.max(shotCount, m.shot_count)
    if ((e.stage === 'frame_done' || e.stage === 'frame_exists') && m.frame_type === 'first_frame' && m.shot_idx != null) framesDone.add(m.shot_idx)
    if ((e.stage === 'video_clip_done' || e.stage === 'video_clip_exists') && m.shot_idx != null) clipsDone.add(m.shot_idx)
    if (STAGE_CN[e.stage]) steps++
  }
  let label = STAGE_CN[lastStage] || lastMsg || '处理中…'
  let frac = null
  if (shotCount) {
    if (clipsDone.size) { label = '生成视频片段 ' + clipsDone.size + '/' + shotCount; frac = clipsDone.size / shotCount }
    else if (framesDone.size) { label = '生成关键帧 ' + framesDone.size + '/' + shotCount; frac = framesDone.size / shotCount }
  }
  return { label, frac, steps }
}

const _SHOT = { pending: '', frame_gen: 'framegen gen', frame_done: 'framedone', video_gen: 'videogen gen', video_done: 'videodone' }
const _TIP = { pending: '待生成', frame_gen: '出关键帧…', frame_done: '帧就绪', video_gen: '生成视频…', video_done: '已完成' }

export function shotGrid(prog) {
  prog = prog || []
  let total = 0; const by = {}
  const bump = (i, st) => { const rank = { pending: 0, frame_gen: 1, frame_done: 2, video_gen: 3, video_done: 4 }; if ((rank[st] || 0) >= (rank[by[i]] || 0)) by[i] = st }
  for (const e of prog) {
    const m = e.meta || {}; if (m.shot_count) total = Math.max(total, m.shot_count)
    const i = m.shot_idx
    if (i == null) continue
    if (e.stage === 'frame_start' && (m.frame_type === 'first_frame' || !m.frame_type)) bump(i, 'frame_gen')
    else if ((e.stage === 'frame_done' || e.stage === 'frame_exists') && m.frame_type === 'first_frame') bump(i, 'frame_done')
    else if (e.stage === 'video_clip_start') bump(i, 'video_gen')
    else if (e.stage === 'video_clip_done' || e.stage === 'video_clip_exists') bump(i, 'video_done')
  }
  if (!total || total > 300) return null
  const cells = []
  for (let i = 0; i < total; i++) { const st = by[i] || 'pending'; cells.push({ cls: _SHOT[st], tip: '镜 ' + i + '：' + _TIP[st], label: st === 'video_done' ? '✓' : (i + 1) }) }
  const vals = Object.values(by)
  const frames = vals.filter((s) => s === 'frame_done' || s === 'video_gen' || s === 'video_done').length
  const vids = vals.filter((s) => s === 'video_done').length
  return { cells, total, frames, vids }
}
