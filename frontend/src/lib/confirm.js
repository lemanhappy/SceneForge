import { reactive } from 'vue'

// Shared styled confirm dialog (replaces the legacy imperative confirmModal()).
// Usage: const ok = await confirmModal('删除？', { okText:'删除', danger:true })
export const confirmState = reactive({
  open: false, message: '', okText: '确认', cancelText: '取消', danger: false, _resolve: null,
})

export function confirmModal(message, opts = {}) {
  return new Promise((resolve) => {
    confirmState.message = message
    confirmState.okText = opts.okText || '确认'
    confirmState.cancelText = opts.cancelText || '取消'
    confirmState.danger = !!opts.danger
    confirmState.open = true
    confirmState._resolve = resolve
  })
}

export function resolveConfirm(v) {
  confirmState.open = false
  const r = confirmState._resolve
  confirmState._resolve = null
  if (r) r(v)
}
