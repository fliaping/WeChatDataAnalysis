import { mount, flushPromises } from '@vue/test-utils'
import { computed, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Workbench from '~/components/AdvancedWorkbench.vue'
import { PRO_ITEMS } from '~/lib/pro-demos/catalog.js'

vi.mock('~/stores/chatAccounts', () => ({ useChatAccountsStore: () => ({ selectedAccount: 'self' }) }))

let api, context, wrapper, blockedFeatures, route
const button = text => wrapper.findAll('button').find(b => b.text() === text)
const field = text => wrapper.findAll('label').find(l => l.text().startsWith(text)).find('input,textarea,select')
const open = async feature => {
  context.value = { feature, account: 'self', username: 'friend', message_id: 'message_0:Msg_fixture:1', text: 'original' }
  wrapper = mount(Workbench, { global: { stubs: { NuxtLink: { template: '<a><slot /></a>' } } } })
  await flushPromises()
}

beforeEach(() => {
  context = ref({})
  route = reactive({ query: {} })
  blockedFeatures = ['send-image', 'contact-search']
  api = vi.fn(async (path, options) => {
    if (path === '/settings') return { origin: 'http://127.0.0.1:8766', harness_url: '/assistant/', token_configured: true }
    if (path === '/capabilities') return { counts: { available: 31, total: 79 }, features: Object.fromEntries(PRO_ITEMS.map(i => [i.key, { state: blockedFeatures.includes(i.key) ? 'blocked' : 'available', reason: blockedFeatures.includes(i.key) ? '缺少驱动' : '' }])) }
    if (path.startsWith('/attachments/')) return { path: '/config/uploads/private/photo.png', filename: 'photo.png', size: 8 }
    if (path === '/actions') return { actions: [] }
    if (path === '/tasks' && !options) return { tasks: [] }
    if (path === '/edits' && options?.method !== 'POST') return { edits: [] }
    if (path === '/edits/preview') return { count: 1, revision: 'revision', warning: '解密副本' }
    if (path === '/edits') return { status: 'applied' }
    if (path === '/actions/prepare') return { id: 'action', status: 'prepared', payload: options.body.payload }
    if (path.endsWith('/commit')) return { id: 'action', status: 'sent', payload: {} }
    return { id: 'task', status: 'draft' }
  })
  for (const [name, value] of Object.entries({ ref, shallowRef, computed, watch, onMounted, useApiBase: () => '/api',
    useRoute: () => route, useAdvancedApi: () => api, useAdvancedFeaturesDialog: () => ({ context, closeDialog: vi.fn() }) })) vi.stubGlobal(name, value)
})
afterEach(() => { wrapper?.unmount(); vi.unstubAllGlobals() })

describe('free advanced workbench', () => {
  it('creates a refresh-and-save Moments draft with no interaction steps', async () => {
    await open('automation-moments-sync')
    expect(wrapper.findAll('label').some(l => l.text().startsWith('执行步骤'))).toBe(false)
    expect(wrapper.text()).toContain('不会点赞、评论、转发或自动回复')
    await field('保存范围').setValue('friend')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path, options]) => path === '/tasks' && options?.method === 'POST')[1].body).toEqual({ kind: 'automation-moments-sync', account: 'self', targets: ['friend'], steps: [], interval: 30, format: 'html' })
    expect(api.mock.calls.some(([path]) => path.endsWith('/activate'))).toBe(false)
  })

  it('uses route links and refreshes an existing workbench context', async () => {
    route.query.feature = 'automation-moments-sync'
    await open('send-text')
    expect(wrapper.find('h1').text()).toBe('朋友圈连续同步')
    context.value = { feature: 'edit-text', account: 'other', username: 'exact', message_id: 'message_0:Msg_fixture:2', text: 'draft' }
    await flushPromises()
    expect(field('账户目录名').element.value).toBe('other')
    expect(field('文字内容').element.value).toBe('draft')
  })

  it('uploads a broadcast video and includes explicit recipient navigation', async () => {
    await open('automation-broadcast')
    await field('群发内容类型').setValue('send-video')
    await field('收件人 / 关注人').setValue('first\nsecond')
    const file = new File(['video fixture'], 'clip.mp4', { type: 'video/mp4' })
    const input = wrapper.find('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: [file] })
    await input.trigger('change')
    await button('上传附件').trigger('click')
    await flushPromises()
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    const body = api.mock.calls.find(([path, options]) => path === '/tasks' && options?.method === 'POST')[1].body
    expect(body.steps).toEqual([{ feature: 'chat-mark-read', payload: { parameters: {} } }, { feature: 'send-video', payload: { parameters: { path: '/config/uploads/private/photo.png' } } }])
    expect(api.mock.calls.some(([path]) => path === '/actions/prepare')).toBe(false)
  })

  it('reads labels without preparing or committing a mutation', async () => {
    await open('contact-list-labels')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/read/labels')).toEqual(['/read/labels', { query: { account: 'self', username: 'friend' } }])
    expect(api.mock.calls.some(([path]) => path === '/actions/prepare')).toBe(false)
  })

  it('preserves full label names and an explicit empty replacement', async () => {
    await open('contact-set-labels')
    await field('联系人标签').setValue('VIP Customers\nFriends')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/actions/prepare')[1].body.payload.parameters).toEqual({ labels: ['VIP Customers', 'Friends'] })
    await field('联系人标签').setValue('')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.filter(([path]) => path === '/actions/prepare').at(-1)[1].body.payload.parameters).toEqual({ labels: [] })
  })

  it('renames the reviewed exact label and describes global deletion scope', async () => {
    await open('contact-rename-label')
    await field('原标签名称').setValue('Old Name')
    await field('改为标签名称').setValue('New Name')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/actions/prepare')[1].body.payload.parameters).toEqual({ name: 'Old Name', new_name: 'New Name' })
    await wrapper.findAll('aside button').find(b => b.text().includes('删除联系人标签')).trigger('click')
    expect(wrapper.text()).toContain('会影响所有使用该标签的联系人')
  })

  it('provides explicit navigation before the onboarding mutation steps', async () => {
    await open('automation-onboarding')
    const steps = JSON.parse(field('执行步骤 JSON').element.value)
    expect(steps.map(step => step.feature)).toEqual(['chat-mark-read', 'contact-remark', 'contact-set-labels', 'send-text'])
    expect(api.mock.calls.some(([path]) => path.endsWith('/activate'))).toBe(false)
  })

  it('renders all 79 features and blocks a missing real driver', async () => {
    await open('send-image')
    expect(wrapper.findAll('aside button')).toHaveLength(79)
    expect(button('准备操作').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('缺少驱动')
    expect(api.mock.calls.some(([path]) => path === '/actions/prepare')).toBe(false)
  })

  it('requires preview and carries its revision into a real edit request', async () => {
    await open('edit-text')
    expect(button('确认修改解密副本').attributes('disabled')).toBeDefined()
    await button('预览影响范围').trigger('click')
    await flushPromises()
    await field('文字内容').setValue('changed')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    const submitted = api.mock.calls.find(([path, options]) => path === '/edits' && options?.method === 'POST')[1].body
    expect(submitted).toMatchObject({ feature: 'edit-text', account: 'self', username: 'friend', text: 'changed', revision: 'revision', message_id: 'message_0:Msg_fixture:1' })
    expect(wrapper.text()).toContain('解密副本已更新')
  })

  it('prepares first and commits only the saved action ID', async () => {
    await open('send-text')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.some(([path]) => path.endsWith('/commit'))).toBe(false)
    await field('发送文字').setValue('different draft')
    expect(wrapper.find('.confirmation pre').text()).toContain('original')
    await button('确认执行以上操作').trigger('click')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path.endsWith('/commit'))).toEqual(['/actions/action/commit', { method: 'POST' }])
  })

  it('edits actual message XML instead of the unrelated source metadata', async () => {
    await open('edit-source')
    await button('预览影响范围').trigger('click')
    await flushPromises()
    await field('消息 XML 源码').setValue('<msg><title>fixed</title></msg>')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    const body = api.mock.calls.find(([path, options]) => path === '/edits' && options?.method === 'POST')[1].body
    expect(body.xml).toBe('<msg><title>fixed</title></msg>')
    expect(body).not.toHaveProperty('source')
  })

  it('captures a complete account contact snapshot without a conversation', async () => {
    await open('contact-insights')
    expect(wrapper.findAll('label').some(l => l.text().startsWith('联系人 / 群 username'))).toBe(false)
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/insights')).toEqual(['/insights', { method: 'POST', body: { account: 'self' } }])
  })

  it('splits CRLF target lists and creates an inactive task', async () => {
    await open('automation-broadcast')
    await field('收件人 / 关注人').setValue('first\r\nsecond\n')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    const body = api.mock.calls.find(([path, options]) => path === '/tasks' && options?.method === 'POST')[1].body
    expect(body.targets).toEqual(['first', 'second'])
    expect(api.mock.calls.some(([path]) => path.endsWith('/activate'))).toBe(false)
  })

  it('uploads separately and prepares the uploaded bytes for the exact recipient', async () => {
    blockedFeatures = ['contact-search']
    await open('send-image')
    expect(button('准备操作').attributes('disabled')).toBeDefined()
    const file = new File(['fixture'], 'photo.png', { type: 'image/png' })
    const input = wrapper.find('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: [file] })
    await input.trigger('change')
    await button('上传附件').trigger('click')
    await flushPromises()
    const upload = api.mock.calls.find(([path]) => path.startsWith('/attachments/'))
    expect(upload[1].body).toBe(file)
    expect(upload[1].method).toBe('PUT')
    expect(api.mock.calls.some(([path]) => path === '/actions/prepare')).toBe(false)
    expect(wrapper.text()).toContain('尚未发送微信消息')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/actions/prepare')[1].body).toMatchObject({ feature: 'send-image', payload: { account: 'self', username: 'friend', parameters: { path: '/config/uploads/private/photo.png' } } })
    expect(api.mock.calls.some(([path]) => path.endsWith('/commit'))).toBe(false)
  })

  it('preserves false as a boolean in the mute form', async () => {
    await open('chat-set-mute')
    await field('消息通知').setValue('false')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    expect(api.mock.calls.find(([path]) => path === '/actions/prepare')[1].body.payload).toEqual({ account: 'self', username: 'friend', parameters: { muted: false } })
  })

  it('prepares a remark through the labeled form and preserves the original confirmation', async () => {
    await open('contact-remark')
    await field('联系人备注').setValue('Approved remark')
    await wrapper.find('main > form').trigger('submit')
    await flushPromises()
    await field('联系人备注').setValue('Another draft')
    expect(api.mock.calls.find(([path]) => path === '/actions/prepare')[1].body.payload.parameters).toEqual({ text: 'Approved remark' })
    expect(wrapper.find('.confirmation').text()).toContain('Approved remark')
    expect(wrapper.find('.confirmation').text()).not.toContain('Another draft')
  })
})
