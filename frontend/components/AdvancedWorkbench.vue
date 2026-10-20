<template>
  <div class="workbench">
    <aside aria-label="高级功能清单">
      <label>查找功能<input v-model="filter" type="search" placeholder="名称或功能代码" /></label>
      <p class="muted">免费版 · {{ PRO_TOTAL }} 项清单</p>
      <label>实现状态<select v-model="statusFilter"><option value="all">全部功能</option><option value="available">当前可用</option><option value="connection">已实现，待连接</option><option value="pending">尚未实现</option></select></label>
      <template v-for="group in groups" :key="group.key">
        <h2>{{ group.label }}</h2>
        <button v-for="item in group.items" :key="item.key" type="button" :aria-pressed="selected === item.key" @click="select(item.key)"><span>{{ item.name }}</span><small>{{ statusLabel(item.key) }}</small></button>
      </template>
    </aside>
    <main>
      <header><h1>{{ current?.name || '高级功能' }}</h1><button type="button" :disabled="busy" @click="refresh">刷新状态</button></header>
      <p class="muted" role="status">全部功能已解锁。{{ caps?.counts ? '当前可用 ' + caps.counts.available + ' / ' + caps.counts.total + ' 项（含解密副本操作）' : '正在核对连接与功能状态…' }}，各项实现状态见左侧清单。</p>
      <p v-if="error" class="notice error" role="alert">{{ error }}</p>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <p v-if="capability?.reason" class="notice">{{ capability.reason }}</p>
      <details>
        <summary>WeHarbor 与助手连接设置</summary>
        <form @submit.prevent="saveSettings">
          <label>WeHarbor 服务地址<input v-model="settings.origin" type="url" placeholder="https://你的 WeHarbor 域名" required /></label>
          <label>Workspace Bearer 令牌<input v-model="token" type="password" autocomplete="new-password" :placeholder="settings.token_configured ? '已配置，留空保留' : '也可通过服务端环境变量配置'" /></label>
          <label>DeepSeek Harness 页面地址<input v-model="settings.harness_url" placeholder="/assistant/" required /></label>
          <details><summary>自定义 MCP 工具绑定</summary><label>功能代码到实际工具名的 JSON 映射<textarea v-model="bindings" rows="4" spellcheck="false" /></label><p class="muted">只有 WeHarbor 实际发布的工具才可绑定，配置不会增加缺失的微信能力。</p></details>
          <button :disabled="busy" type="submit">保存连接设置</button>
        </form>
      </details>
      <template v-if="selected === 'ai-assistant'"><p>助手使用 DeepSeek Harness，模型、工具与对话历史由 Harness 管理。</p><NuxtLink class="action-link" to="/ai" @click="closeDialog">进入 AI 助手</NuxtLink></template>
      <template v-else-if="selected === 'mcp-access'"><p>DeepSeek Harness 通过 MCP 读取账户、会话、联系人、群成员、消息和朋友圈，准备与提交 Action、查询回执、管理自动化任务。工具资源提供完整 Skill 用法；写操作按用户授权执行。</p><label>MCP HTTP 地址<input :value="mcpAddress" readonly @focus="$event.target.select()" /></label><p>Bearer 令牌保存在服务端数据目录 advanced/mcp.token。Harness 可使用 scripts/advanced_mcp_stdio.py 接入。配置说明：docs/advanced-features.md。</p></template>
      <template v-else>
        <form @submit.prevent="execute">
          <div class="grid"><label>账户目录名<input v-model="account" required /></label><label v-if="needsUsername">联系人 / 群 username<input v-model="username" required placeholder="精确微信 ID 或 xxx@chatroom" /></label></div>
          <template v-if="selected === 'edit-restore'">
            <label>待还原编辑<select v-model="journalId" required><option value="">选择编辑记录</option><option v-for="entry in edits.filter(e => !e.restored)" :key="entry.id" :value="entry.id">{{ new Date(entry.created * 1000).toLocaleString() }} · {{ entry.id.slice(0, 8) }}</option></select></label>
            <button type="button" :disabled="busy || !account" @click="loadHistory">加载编辑记录</button>
          </template>
          <template v-else-if="isLocal">
            <label v-if="!isAdd">消息 ID {{ batch ? '（留空选择会话内消息）' : '' }}<input v-model="messageId" :required="!batch" placeholder="message_0:Msg_xxx:123" /></label>
            <div v-if="batch" class="grid"><label>起始 Unix 秒<input v-model="startTime" type="number" min="1" /></label><label>截止 Unix 秒<input v-model="endTime" type="number" min="1" /></label></div>
            <label v-if="needsText">{{ selected === 'edit-source' ? '消息 XML 源码' : '文字内容' }}<textarea v-model="text" rows="5" required /></label>
            <label v-if="needsTime">消息时间<input v-model="messageTime" type="datetime-local" required /></label>
            <label v-if="isAdd">发送者 username<input v-model="sender" placeholder="默认会话联系人；本人消息填写本人微信 ID" /></label>
            <template v-if="needsXml"><label>原消息 XML<textarea v-model="xml" rows="9" required spellcheck="false" /></label><p class="muted">保留原资源标识和消息结构。媒体文件须已在本地可用；补录不会上传资源或产生真实支付。</p></template>
            <label v-if="selected === 'edit-fields' || selected === 'edit-fix-sender'">{{ selected === 'edit-fields' ? '字段 JSON' : '发送者 rowid 映射 JSON' }}<textarea v-model="parameters" rows="6" required spellcheck="false" /></label>
            <button v-if="!isAdd" type="button" :disabled="busy" @click="previewEdit">预览影响范围</button>
            <p v-if="preview" class="notice">预览：{{ preview.count }} 条消息。{{ preview.warning }}</p>
          </template>
          <template v-else-if="isTask">
            <label>{{ isMomentsTask ? '保存范围（每行一个 username；留空保存全部可见内容）' : '收件人 / 关注人（每行一个 username）' }}<textarea v-model="targets" rows="4" :required="selected === 'automation-broadcast'" /></label>
            <template v-if="isMomentsTask"><label>保存格式<select v-model="archiveFormat"><option value="html">HTML 网页与媒体</option><option value="json">JSON 数据与媒体</option></select></label><p class="muted">定时刷新微信朋友圈，增量保存内容和可获取的图片、视频。保留已保存内容，重启后继续；不会点赞、评论、转发或自动回复。仅保存微信当前可见、已同步的数据，无法恢复已删除或无权查看的内容。</p></template>
            <template v-else>
              <template v-if="selected === 'automation-broadcast'">
                <label>群发内容类型<select v-model="broadcastType" @change="uploaded = null; attachmentFile = null"><option value="send-text">文字</option><option value="send-image">图片</option><option value="send-video">视频</option><option value="send-file">文件</option></select></label>
                <label v-if="broadcastType === 'send-text'">群发原文<textarea v-model="text" rows="5" required maxlength="4000" /></label>
                <template v-else><label>选择群发附件（最大 100 MiB）<input type="file" :accept="attachmentAccept" :disabled="busy || blocked" @change="chooseAttachment" /></label><button type="button" :disabled="busy || blocked || !attachmentFile || !!uploaded" @click="uploadAttachment">{{ uploaded ? '附件已上传' : '上传附件' }}</button><p class="muted">{{ uploaded ? uploaded.filename + '，尚未发送。' : '上传附件后再创建任务。' }}</p></template>
                <label class="check"><input v-model="navigateTargets" type="checkbox" />每个目标发送前打开聊天并标记已读</label>
                <label class="check"><input v-model="customSteps" type="checkbox" />使用自定义步骤</label>
              </template>
              <label v-if="selected === 'automation-onboarding' || customSteps">执行步骤 JSON<textarea v-model="taskSteps" rows="9" required spellcheck="false" /></label>
            <template v-if="selected === 'automation-onboarding'"><button type="button" :disabled="busy" @click="taskSteps = JSON.stringify(onboardingSteps, null, 2)">载入迎新步骤示例</button><p class="muted">只处理启动后首次出现在完整通讯录中的好友。示例会先打开目标聊天并标记已读，再备注、设置标签和发送欢迎语；请先创建“新朋友”标签并修改示例内容。收件人列表留空时适用于所有新好友。</p></template>
              <p class="muted">$account、$username 可作为完整参数值，每个步骤独立记录回执。</p>
            </template>
            <p class="muted">创建后核对下方任务配置，再确认启动。</p>
            <div class="grid"><label>开始时间（本地时区）<input v-model="due" type="datetime-local" /></label><label>{{ isMomentsTask ? '同步间隔（秒，至少 30）' : '步骤 / 检查间隔（秒）' }}<input v-model="interval" type="number" :min="isMomentsTask ? 30 : 5" max="86400" required /></label></div>
          </template>
          <template v-else-if="selected === 'send-text'"><label>发送文字<textarea v-model="text" rows="5" required maxlength="4000" /></label><label>定时发送（可留空，本地时区）<input v-model="due" type="datetime-local" /></label><p class="muted">在 WeHarbor 桌面打开对应账户和聊天，保留空草稿。准备成功后核对收件人与原文，再确认提交。</p></template>
          <template v-else-if="isAttachment">
            <label>选择附件（最大 100 MiB）<input type="file" :accept="attachmentAccept" :disabled="busy || blocked" @change="chooseAttachment" /></label>
            <p v-if="attachmentFile" class="muted">{{ attachmentFile.name }} · {{ Math.ceil(attachmentFile.size / 1024) }} KiB</p>
            <button type="button" :disabled="busy || blocked || !attachmentFile || !!uploaded" @click="uploadAttachment">{{ uploaded ? '附件已上传' : '上传附件' }}</button>
            <p class="muted" role="status">{{ uploaded ? '上传完成。请核对目标，再准备发送。' : '先上传附件，再准备并确认发送。上传本身不会发送微信消息。' }}</p>
          </template>
          <template v-else-if="selected === 'chat-set-mute'"><label>消息通知<select v-model="muted"><option :value="true">开启免打扰</option><option :value="false">关闭免打扰</option></select></label></template>
          <template v-else-if="isLabel">
            <label v-if="selected === 'contact-set-labels'">联系人标签（每行一个完整名称）<textarea v-model="labelNames" rows="5" /><small>填写最终标签集合；留空会清除该联系人的全部标签。请先创建不存在的标签。</small></label>
            <label v-else-if="selected !== 'contact-list-labels'">{{ selected === 'contact-create-label' ? '新标签名称' : '原标签名称' }}<input v-model="labelName" required maxlength="64" /></label>
            <label v-if="selected === 'contact-rename-label'">改为标签名称<input v-model="newLabelName" required maxlength="64" /></label>
            <p class="muted">在微信桌面打开作为标签入口的联系人聊天。{{ selected === 'contact-delete-label' ? '删除标签会影响所有使用该标签的联系人，但不会删除联系人。' : selected === 'contact-list-labels' ? '读取微信完整标签列表，不修改标签。' : '标签更改后会重新打开界面核对保存结果。' }}</p>
          </template>
          <template v-else-if="isSetting"><label>{{ settingLabel }}<textarea v-model="text" rows="4" :maxlength="selected === 'group-notice' ? 2000 : 128" :required="selected === 'group-rename'" /></label><p class="muted">在微信桌面打开对应聊天。提交后会重新打开聊天资料，读取已保存的值。</p></template>
          <template v-else-if="selected === 'chat-mark-read'"><p class="muted">确认后打开这个精确会话，使微信将其标为已读；准备阶段不会切换聊天。</p></template>
          <template v-else-if="isRead"><p v-if="selected === 'contact-insights'" class="muted">保存当前完整通讯录快照，并与上次快照比较。首次只建立基线，此后服务每小时检查一次。</p></template>
          <template v-else-if="selected === 'sns-autorefresh'"><p class="muted">刷新本机已登录账户的微信朋友圈并同步快照。需要微信提供可访问的朋友圈刷新按钮；持续保存请使用“朋友圈连续同步”。</p></template>
          <template v-else><label>工具参数 JSON<textarea v-model="parameters" rows="8" required spellcheck="false" /></label><details v-if="capability?.schema"><summary>参数定义</summary><pre>{{ JSON.stringify(capability.schema, null, 2) }}</pre></details></template>
          <button class="primary submit" type="submit" :disabled="busy || blocked || (isAttachment && !uploaded) || (isLocal && !isAdd && selected !== 'edit-restore' && !preview)">{{ busy ? '处理中…' : isRead ? '查询' : isLocal ? '确认修改解密副本' : isTask ? '创建待启动任务' : selected === 'sns-autorefresh' ? '刷新朋友圈' : '准备操作' }}</button>
        </form>
        <section v-if="prepared" class="confirmation" aria-label="操作确认"><h2>核对后提交</h2><p>状态：{{ prepared.status }} · {{ prepared.id }}</p><pre>{{ JSON.stringify(prepared.payload, null, 2) }}</pre><button v-if="prepared.status === 'prepared'" class="primary" type="button" :disabled="busy" @click="commitAction">确认执行以上操作</button><button v-if="['prepared', 'scheduled'].includes(prepared.status)" type="button" :disabled="busy" @click="cancelAction">取消操作</button></section>
        <details v-if="result" open><summary>操作结果</summary><pre>{{ JSON.stringify(result, null, 2) }}</pre></details>
        <section class="history"><h2>任务与操作记录</h2><button type="button" :disabled="busy" @click="loadHistory">刷新回执</button>
          <article v-for="task in taskHistory" :key="task.id"><p>{{ taskName(task.value.kind) }} · {{ taskStatus(task.status) }} · {{ task.result }}</p><small>{{ task.id }} · {{ task.value.kind === 'automation-moments-sync' ? '已保存 ' + task.cursor + ' 轮' : '已处理 ' + task.cursor + ' / ' + task.value.queue.length + ' 步' }}</small><p v-if="task.value.output_dir" class="muted">保存位置：{{ task.value.output_dir }}/archive</p><details><summary>查看任务配置与保存结果</summary><pre>{{ JSON.stringify(task.value, null, 2) }}</pre></details><div class="button-row"><button v-if="task.status === 'draft' && task.value.kind !== 'automation-moments-follow'" type="button" :disabled="busy" @click="taskOperation(task, 'activate')">确认启动</button><button v-if="task.status === 'active'" type="button" :disabled="busy" @click="taskOperation(task, 'pause')">暂停</button><button v-if="task.status === 'paused' && task.value.kind !== 'automation-moments-follow'" type="button" :disabled="busy" @click="taskOperation(task, 'resume')">继续</button><button v-if="['draft','active','paused'].includes(task.status)" type="button" :disabled="busy" @click="taskOperation(task, 'cancel')">取消任务</button></div></article>
          <article v-for="action in actionHistory" :key="action.id"><p>{{ action.feature }} · {{ action.status }}</p><small>{{ action.id }} · {{ new Date(action.created * 1000).toLocaleString() }}</small><pre>{{ JSON.stringify(action.result, null, 2) }}</pre></article>
          <p v-if="!actionHistory.length && !taskHistory.length" class="muted">还没有操作记录。</p>
        </section>
      </template>
    </main>
  </div>
</template>
<script setup>
import { PRO_GROUPS, PRO_ITEMS, PRO_TOTAL } from '~/lib/pro-demos/catalog.js'
import { useChatAccountsStore } from '~/stores/chatAccounts'
const api = useAdvancedApi(), apiBase = useApiBase()
const { context, closeDialog } = useAdvancedFeaturesDialog()
const accountStore = useChatAccountsStore()
const route = useRoute()
const initialFeature = route.query.feature || context.value.feature
const selected = ref(PRO_ITEMS.some(i => i.key === initialFeature) ? initialFeature : 'send-text'), filter = ref(''), statusFilter = ref('all')
const account = ref(context.value.account || accountStore.selectedAccount || ''), username = ref(context.value.username || '')
const messageId = ref(context.value.message_id || ''), text = ref(context.value.text || ''), xml = ref(''), sender = ref(''), parameters = ref('{}')
const messageTime = ref(''), startTime = ref(''), endTime = ref(''), due = ref('')
const interval = ref(30), targets = ref(''), archiveFormat = ref('html'), broadcastType = ref('send-text'), navigateTargets = ref(true), customSteps = ref(false)
const onboardingSteps = [{ feature: 'chat-mark-read', payload: { parameters: {} } }, { feature: 'contact-remark', payload: { parameters: { text: '新朋友' } } }, { feature: 'contact-set-labels', payload: { parameters: { labels: ['新朋友'] } } }, { feature: 'send-text', payload: { text: '请填写欢迎语' } }]
const taskSteps = ref(JSON.stringify(selected.value === 'automation-onboarding' ? onboardingSteps : [{ feature: 'send-text', payload: { text: '请填写要发送的原文', username: '$username', account: '$account' } }], null, 2))
const caps = ref(null), settings = ref({ origin: '', harness_url: '/assistant/' }), token = ref(''), bindings = ref('{}')
const busy = ref(false), error = ref(''), notice = ref(''), preview = ref(null), prepared = ref(null), result = ref(null), journalId = ref('')
const edits = ref([]), actionHistory = ref([]), taskHistory = ref([])
const attachmentFile = shallowRef(null), uploaded = ref(null), muted = ref(true)
const labelName = ref(''), newLabelName = ref(''), labelNames = ref('')
const groups = computed(() => PRO_GROUPS.map(g => ({ ...g, items: g.items.filter(i => (i.name + i.key).toLowerCase().includes(filter.value.toLowerCase()) && (statusFilter.value === 'all' || featureState(i.key) === statusFilter.value)) })).filter(g => g.items.length))
const current = computed(() => PRO_ITEMS.find(i => i.key === selected.value)), capability = computed(() => caps.value?.features?.[selected.value])
const blocked = computed(() => capability.value?.state !== 'available'), isLocal = computed(() => current.value?.local)
const isAdd = computed(() => selected.value.startsWith('add-')), isTask = computed(() => selected.value.startsWith('automation-'))
const isMomentsTask = computed(() => selected.value === 'automation-moments-sync')
const batch = computed(() => ['edit-fix-sender', 'edit-flip-sides', 'edit-delete-sys'].includes(selected.value))
const isRead = computed(() => ['receive-messages', 'contact-insights', 'contact-list-labels'].includes(selected.value))
const isLabel = computed(() => ['contact-create-label', 'contact-set-labels', 'contact-list-labels', 'contact-rename-label', 'contact-delete-label'].includes(selected.value))
const isAttachment = computed(() => ['send-file', 'send-image', 'send-video'].includes(selected.value))
const isSetting = computed(() => ['contact-remark', 'group-rename', 'group-my-nick', 'group-notice'].includes(selected.value))
const settingLabel = computed(() => ({ 'contact-remark': '联系人备注', 'group-rename': '群聊名称', 'group-my-nick': '我在本群的昵称', 'group-notice': '群公告' })[selected.value])
const attachmentAccept = computed(() => ({ 'send-image': 'image/jpeg,image/png,image/gif,image/bmp,image/webp', 'send-video': 'video/mp4,video/quicktime,video/webm' })[selected.value === 'automation-broadcast' ? broadcastType.value : selected.value])
const needsUsername = computed(() => (isLocal.value && selected.value !== 'edit-restore') || isAttachment.value || isSetting.value || isLabel.value || ['send-text', 'receive-messages', 'chat-set-mute', 'chat-mark-read'].includes(selected.value))
const needsText = computed(() => ['edit-text', 'edit-source', 'add-text', 'add-sys', 'add-pat'].includes(selected.value))
const needsTime = computed(() => selected.value === 'edit-time' || isAdd.value)
const needsXml = computed(() => isAdd.value && !['add-text', 'add-sys', 'add-pat'].includes(selected.value))
const mcpAddress = computed(() => typeof window === 'undefined' ? apiBase + '/advanced/mcp' : new URL(apiBase + '/advanced/mcp', window.location.origin).href)
const featureState = key => caps.value?.features?.[key]?.state === 'available' ? 'available' : caps.value?.features?.[key]?.implementation === 'implemented' ? 'connection' : 'pending'
const statusLabel = key => !caps.value ? '待检查' : ({ available: '可用', connection: '已实现，待连接', pending: '尚未实现' })[featureState(key)]
const taskName = kind => PRO_ITEMS.find(i => i.key === kind)?.name || (kind === 'automation-moments-follow' ? '旧朋友圈互动（已停用）' : kind)
const taskStatus = status => ({draft:'待启动',active:'运行中',paused:'已暂停',cancelled:'已取消',completed:'已完成'})[status] || status
const lines = value => value.split(/\r?\n/).map(v => v.trim()).filter(Boolean)
const unix = value => { const stamp = new Date(value).getTime() / 1000; if (!Number.isFinite(stamp)) throw new Error('请填写有效时间'); return Math.floor(stamp) }
const guard = async fn => { if (busy.value) return; busy.value = true; error.value = ''; notice.value = ''; try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false } }
const select = key => { selected.value = PRO_ITEMS.some(i => i.key === key) ? key : 'send-text'; result.value = null; prepared.value = null; preview.value = null; attachmentFile.value = null; uploaded.value = null; error.value = ''; notice.value = ''; if (key === 'automation-onboarding') taskSteps.value = JSON.stringify(onboardingSteps, null, 2) }
watch(() => route.query.feature, key => { if (key) select(key) })
watch(context, value => { if (value.feature) { select(value.feature); account.value = value.account || accountStore.selectedAccount || ''; username.value = value.username || ''; messageId.value = value.message_id || ''; text.value = value.text || '' } })
const chooseAttachment = event => { attachmentFile.value = event.target.files?.[0] || null; uploaded.value = null; error.value = ''; notice.value = '' }
const uploadAttachment = () => guard(async () => {
  const file = attachmentFile.value
  if (!file || !file.size || file.size > 100 * 1024 * 1024) throw new Error('请选择非空且不超过 100 MiB 的附件')
  notice.value = '正在上传附件，请稍候…'
  uploaded.value = await api('/attachments/' + encodeURIComponent(file.name), { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file })
  notice.value = '附件已上传，尚未发送微信消息。'
})
watch([account, username, messageId, selected, startTime, endTime], () => { preview.value = null })
const editPayload = () => ({ feature: selected.value, account: account.value, username: username.value, ...(messageId.value ? { message_id: messageId.value } : {}), ...(startTime.value ? { start_time: Number(startTime.value) } : {}), ...(endTime.value ? { end_time: Number(endTime.value) } : {}) })
const refreshData = async () => { const [cfg, capabilities] = await Promise.all([api('/settings'), api('/capabilities')]); settings.value = cfg; bindings.value = JSON.stringify(cfg.tool_bindings || {}, null, 2); caps.value = capabilities }
const refresh = () => guard(refreshData)
const saveSettings = () => guard(async () => { await api('/settings', { method: 'PUT', body: { origin: settings.value.origin, harness_url: settings.value.harness_url, token: token.value, tool_bindings: JSON.parse(bindings.value) } }); token.value = ''; await refreshData(); notice.value = '连接设置已保存' })
const historyData = async () => { const [actions, tasks, journal] = await Promise.all([api('/actions'), api('/tasks'), account.value ? api('/edits', { query: { account: account.value } }) : Promise.resolve({ edits: [] })]); actionHistory.value = actions.actions; taskHistory.value = tasks.tasks; edits.value = journal.edits }
const loadHistory = () => guard(historyData)
const previewEdit = () => guard(async () => { preview.value = await api('/edits/preview', { method: 'POST', body: editPayload() }); result.value = preview.value })
const execute = () => guard(async () => {
  if (isLocal.value) {
    const payload = editPayload()
    if (selected.value === 'edit-restore') payload.journal_id = journalId.value
    else if (!isAdd.value) payload.revision = preview.value?.revision
    if (needsText.value) payload[selected.value === 'edit-source' ? 'xml' : 'text'] = text.value
    if (needsTime.value) payload.create_time = unix(messageTime.value)
    if (isAdd.value) { payload.sender = sender.value; if (needsXml.value) payload.xml = xml.value }
    if (selected.value === 'edit-fields') payload.fields = JSON.parse(parameters.value)
    if (selected.value === 'edit-fix-sender') payload.mapping = JSON.parse(parameters.value)
    result.value = await api('/edits', { method: 'POST', body: payload }); preview.value = null
    notice.value = '解密副本已更新，返回聊天并刷新数据即可查看。'; await historyData()
  } else if (isRead.value) {
    result.value = selected.value === 'contact-insights'
      ? await api('/insights', { method: 'POST', body: { account: account.value } })
      : await api(selected.value === 'contact-list-labels' ? '/read/labels' : '/read/messages', { query: { account: account.value, username: username.value, ...(selected.value === 'receive-messages' ? { limit: 100 } : {}) } })
  } else if (isTask.value) {
    let steps = []
    if (!isMomentsTask.value) {
      if (selected.value === 'automation-onboarding' || customSteps.value) steps = JSON.parse(taskSteps.value)
      else {
        if (caps.value?.features?.[broadcastType.value]?.state !== 'available') throw new Error('当前实例不支持所选群发内容类型')
        if (broadcastType.value !== 'send-text' && !uploaded.value?.path) throw new Error('请先上传群发附件')
        if (navigateTargets.value) steps.push({ feature: 'chat-mark-read', payload: { parameters: {} } })
        steps.push({ feature: broadcastType.value, payload: broadcastType.value === 'send-text' ? { text: text.value } : { parameters: { path: uploaded.value.path } } })
      }
    }
    result.value = await api('/tasks', { method: 'POST', body: { kind: selected.value, account: account.value, targets: lines(targets.value), steps, interval: Number(interval.value), ...(isMomentsTask.value ? { format: archiveFormat.value } : {}), ...(due.value ? { due: unix(due.value) } : {}) } }); await historyData(); notice.value = '任务已创建，核对配置后点击“确认启动”'
  } else if (selected.value === 'sns-autorefresh') {
    result.value = await api('/moments/refresh', { method: 'POST', body: { account: account.value } })
  } else {
    if (isAttachment.value && !uploaded.value?.path) throw new Error('请先上传附件')
    const labelParameters = selected.value === 'contact-set-labels' ? { labels: lines(labelNames.value) } : { name: labelName.value.trim(), ...(selected.value === 'contact-rename-label' ? { new_name: newLabelName.value.trim() } : {}) }
    const toolParameters = isLabel.value ? labelParameters : isAttachment.value ? { path: uploaded.value.path } : isSetting.value ? { text: text.value } : selected.value === 'chat-set-mute' ? { muted: muted.value } : selected.value === 'chat-mark-read' ? {} : selected.value === 'send-text' ? {} : JSON.parse(parameters.value)
    const payload = selected.value === 'send-text' ? { account: account.value, username: username.value, text: text.value, ...(due.value ? { due: unix(due.value) } : {}) } : { account: account.value, username: username.value, parameters: toolParameters }
    const key = globalThis.crypto?.randomUUID?.() || 'web-' + Date.now() + '-' + Math.random().toString(36).slice(2)
    prepared.value = await api('/actions/prepare', { method: 'POST', body: { feature: selected.value, payload, idempotency_key: key } }); await historyData()
  }
})
const commitAction = () => guard(async () => { prepared.value = await api('/actions/' + encodeURIComponent(prepared.value.id) + '/commit', { method: 'POST' }); result.value = prepared.value; await historyData() })
const cancelAction = () => guard(async () => { prepared.value = await api('/actions/' + encodeURIComponent(prepared.value.id) + '/cancel', { method: 'POST' }); await historyData() })
const taskOperation = (task, operation) => guard(async () => { await api('/tasks/' + task.id + '/' + operation, { method: 'POST' }); await historyData() })
onMounted(() => guard(async () => { await refreshData(); await historyData() }))
</script>
<style scoped>
.check{flex-direction:row;align-items:center}.check input{width:20px;min-height:20px;flex:none}
.workbench{display:grid;grid-template-columns:240px minmax(0,1fr);height:100%;min-height:0;background:var(--app-surface-bg);color:var(--app-text-primary);font-size:14px}aside,main{overflow:auto;min-height:0;padding:20px}aside{border-right:1px solid var(--app-border);background:var(--app-surface-soft)}aside h2{font-size:12px;color:var(--app-text-secondary);margin:24px 0 8px}aside button{width:100%;justify-content:space-between;text-align:left;margin:2px 0}aside button[aria-pressed=true]{border-color:var(--app-accent);background:var(--app-surface-muted)}main{padding:24px clamp(16px,3vw,40px)}header{display:flex;align-items:center;justify-content:space-between;gap:12px}h1{font-size:22px;margin:0}h2{font-size:16px}p{line-height:1.7}.muted,small{color:var(--app-text-secondary)}small{font-size:11px;overflow-wrap:anywhere}button,.action-link{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:8px 12px;border:1px solid var(--app-border);border-radius:7px;background:var(--app-surface-bg);color:inherit;cursor:pointer;font:inherit;text-decoration:none}button:disabled{opacity:.55;cursor:not-allowed}.primary,.action-link{background:var(--app-accent);border-color:var(--app-accent);color:white}button:hover:not(:disabled){filter:brightness(.95)}button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible,summary:focus-visible,.action-link:focus-visible{outline:2px solid var(--app-accent);outline-offset:3px}label{display:flex;flex-direction:column;gap:8px;margin:12px 0;color:var(--app-text-secondary)}input,textarea,select{box-sizing:border-box;width:100%;min-height:44px;padding:10px 12px;border:1px solid var(--app-input-border);border-radius:7px;background:var(--app-input-bg);color:var(--app-text-primary);font:inherit}textarea{resize:vertical;line-height:1.6}details{margin:16px 0;padding:12px;border:1px solid var(--app-border);border-radius:8px}summary{cursor:pointer;min-height:28px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.submit{margin-top:16px}.notice{padding:12px 16px;border-left:3px solid var(--app-accent);background:var(--app-surface-soft);overflow-wrap:anywhere}.error{border-color:var(--app-danger,#c2413b)}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font-size:12px;line-height:1.6;background:var(--app-surface-soft);padding:12px}.confirmation{border:1px solid var(--app-accent);padding:16px;border-radius:8px;margin:20px 0}.confirmation button{margin:8px 8px 0 0}.history{margin-top:32px}article{border-top:1px solid var(--app-border);padding:16px 0}.button-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}@media(max-width:760px){.workbench{grid-template-columns:170px minmax(0,1fr)}aside{padding:12px}main{padding:16px}.grid{grid-template-columns:1fr;gap:0}aside button{flex-direction:column;align-items:flex-start;gap:2px}}@media(max-width:480px){.workbench{grid-template-columns:1fr;grid-template-rows:150px minmax(0,1fr)}aside{border-right:0;border-bottom:1px solid var(--app-border)}aside h2{margin-top:12px}}
</style>
