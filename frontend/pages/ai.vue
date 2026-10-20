<template>
  <main class="harness-page">
    <header><div><h1>AI 助手</h1><p>DeepSeek Harness · 微信读取、Action 与自动化任务</p></div><NuxtLink to="/advanced">功能与连接设置</NuxtLink></header>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-else-if="!url" role="status" class="loading">正在加载助手设置…</div>
    <template v-else>
      <div class="harness-bar"><span>模型与历史记录由 Harness 管理</span><a :href="url" target="_blank" rel="noopener noreferrer">在新窗口打开</a><button type="button" @click="reload">重新加载</button></div>
      <iframe :key="frameKey" :src="url" title="DeepSeek Harness 微信助手" referrerpolicy="same-origin" @error="error = '无法加载助手，请在连接设置检查 Harness 地址'" />
    </template>
  </main>
</template>
<script setup>
useHead({ title: 'AI 助手 - 微信数据分析助手' })
const api = useAdvancedApi(), url = ref(''), error = ref(''), frameKey = ref(0)
const reload = () => { frameKey.value++; error.value = '' }
onMounted(async () => { try { url.value = (await api('/settings')).harness_url } catch (e) { error.value = e.message } })
</script>
<style scoped>
.harness-page{display:flex;flex-direction:column;height:100%;min-height:0;color:var(--app-text-primary);background:var(--app-surface-bg)}header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;border-bottom:1px solid var(--app-border)}h1{font-size:20px;margin:0}p{font-size:13px;color:var(--app-text-secondary);margin:6px 0}.harness-bar{display:flex;align-items:center;flex-wrap:wrap;gap:16px;padding:8px 24px;font-size:12px;background:var(--app-surface-soft);color:var(--app-text-secondary)}.harness-bar span{flex:1}a,button{display:inline-flex;align-items:center;min-height:44px;padding:4px 8px;color:var(--app-accent);cursor:pointer}button{background:transparent;border:1px solid var(--app-border);border-radius:6px}a:focus-visible,button:focus-visible{outline:2px solid var(--app-accent);outline-offset:2px}iframe{width:100%;flex:1;min-height:0;border:0;background:var(--app-surface-bg)}.loading{padding:24px}
</style>
