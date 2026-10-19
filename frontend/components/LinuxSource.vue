<script setup lang="ts">
const config = useRuntimeConfig()
const status = ref<any>(null)
const busy = ref(false)
const result = ref('')
const captureAccount = ref('')
let timer: ReturnType<typeof setInterval> | undefined
async function refresh() {
  try { status.value = await $fetch(`${config.public.apiBase}/linux/status`) } catch {}
}
onMounted(async () => {
  await refresh()
  captureAccount.value = status.value?.local_accounts?.[0] || ''
})
onUnmounted(() => { if (timer) clearInterval(timer) })
async function capture() {
  busy.value = true
  try {
    await $fetch(`${config.public.apiBase}/linux/capture`, {method:'POST', headers:{'X-WeHarbor-Action':'sync'}, body:{account:captureAccount.value}})
    result.value = '等待你在微信桌面退出账号并重新登录（120 秒）。不会自动操作登录。'
    timer = setInterval(async () => {
      await refresh()
      const job = status.value?.capture
      if (job?.status === 'complete' || job?.status === 'failed') {
        clearInterval(timer); timer = undefined; busy.value = false
        result.value = job.status === 'complete' ? '密钥通过数据库验证，导入完成。' : '捕获失败，请检查 ptrace 权限、微信版本与重新登录操作。'
      }
    }, 3000)
  } catch { busy.value = false; result.value = '无法启动捕获，请检查服务日志。' }
}
async function sync() {
  busy.value = true; result.value = '正在校验密钥并同步数据库，请稍候…'
  try {
    const data = await $fetch<any>(`${config.public.apiBase}/linux/sync`, {method: 'POST', headers: {'X-WeHarbor-Action': 'sync'}})
    result.value = data.accounts.map((a: any) => `${a.account}：更新 ${a.changed.length} 个数据库，${a.failed.length} 个失败；图片密钥${a.image_key_verified ? '已验证' : '待验证'}`).join('；') || '未发现本地 WeFlow 账号'
  } catch { result.value = '同步失败。请检查本地 WeFlow 配置和服务日志。' }
  finally { busy.value = false }
}
</script>
<template>
  <section v-if="status?.supported" class="rounded-lg border border-[#CFEEDB] bg-[#F3FFF8] p-5" aria-labelledby="linux-source-title">
    <h2 id="linux-source-title" class="text-base font-semibold">Linux 数据源</h2>
    <p class="mt-2 text-sm text-gray-600">复用本地 WeFlow 密钥，同步数据库与 WAL；图片密钥通过真实文件校验后启用。</p>
    <div class="mt-3 flex flex-wrap items-center gap-3">
      <button type="button" :disabled="busy || !status.accounts.length" class="min-h-11 rounded-lg bg-[#07C160] px-4 text-sm text-white disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-green-700" @click="sync">{{ busy ? '正在同步…' : '同步本地账号' }}</button>
      <NuxtLink to="/chat" class="rounded px-2 py-3 text-sm text-green-700 underline focus-visible:outline">查看聊天记录</NuxtLink>
      <span class="text-sm text-gray-600">{{ status.accounts.length }} 个可用账号</span>
    </div>
    <p v-if="result" class="mt-3 text-sm text-gray-700" role="status">{{ result }}</p>
    <details v-if="status.capture_available && status.local_accounts.length" class="mt-4 text-sm">
      <summary class="cursor-pointer py-2">没有可用密钥？使用 wcdb-key-tool 捕获</summary>
      <p class="my-2 text-gray-600">仅在原有密钥不可用时使用。开始后需要你在微信桌面手动退出账号并重新登录；GDB 需要 ptrace 权限。</p>
      <label for="linux-capture-account">本地账号</label>
      <select id="linux-capture-account" v-model="captureAccount" class="mx-2 min-h-11 rounded border p-2"><option v-for="account in status.local_accounts" :key="account" :value="account">{{ account }}</option></select>
      <button type="button" :disabled="busy" class="min-h-11 rounded border border-green-700 px-4 disabled:opacity-50" @click="capture">开始捕获，准备手动重新登录</button>
    </details>
  </section>
</template>
