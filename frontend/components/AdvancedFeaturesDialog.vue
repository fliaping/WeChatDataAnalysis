<template>
  <Teleport to="body">
    <div v-if="open" class="advanced-overlay" @mousedown.self="emit('close')">
      <section ref="panel" class="advanced-panel" role="dialog" aria-modal="true" aria-label="免费版高级功能" tabindex="-1" @keydown="onKeydown">
        <header><span>免费版高级功能</span><button type="button" aria-label="关闭高级功能" @click="emit('close')">关闭 · Esc</button></header>
        <AdvancedWorkbench />
      </section>
    </div>
  </Teleport>
</template>
<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
const props = defineProps({ open: Boolean })
const emit = defineEmits(['close', 'open-plans'])
const panel = ref(null)
let previousFocus, previousOverflow
const restore = () => { if (typeof document === 'undefined' || previousOverflow === undefined) return; document.body.style.overflow = previousOverflow; previousOverflow = undefined; previousFocus?.focus?.() }
watch(() => props.open, async value => {
  if (!import.meta.client) return
  if (!value) { restore(); return }
  previousFocus = document.activeElement
  previousOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  await nextTick(); panel.value?.focus()
}, { immediate: true })
const onKeydown = event => {
  if (event.key === 'Escape') { event.stopPropagation(); emit('close') }
  if (event.key !== 'Tab') return
  const nodes = [...panel.value.querySelectorAll('button:not(:disabled),a[href],input,textarea,select,summary,[tabindex="0"]')].filter(el => el.getClientRects().length)
  const first = nodes[0], last = nodes.at(-1)
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) { event.preventDefault(); first?.focus() }
}
onBeforeUnmount(restore)
</script>
<style scoped>
.advanced-overlay{position:fixed;inset:0;z-index:1000;background:rgb(0 0 0 / .5);display:flex;align-items:center;justify-content:center;padding:20px}.advanced-panel{display:flex;flex-direction:column;width:min(1200px,100%);height:min(900px,94dvh);min-height:0;background:var(--app-surface-bg);color:var(--app-text-primary);border:1px solid var(--app-border);border-radius:12px;overflow:hidden}.advanced-panel>header{display:flex;align-items:center;justify-content:space-between;flex:none;height:60px;padding:8px 20px;border-bottom:1px solid var(--app-border)}button{min-height:44px;padding:8px 12px;border:1px solid var(--app-border);border-radius:6px;background:var(--app-surface-soft);color:inherit;cursor:pointer}button:focus-visible{outline:2px solid var(--app-accent);outline-offset:2px}.advanced-panel>:deep(.workbench){flex:1;min-height:0}@media(max-width:600px){.advanced-overlay{padding:0}.advanced-panel{height:100dvh;border-radius:0}}
</style>
