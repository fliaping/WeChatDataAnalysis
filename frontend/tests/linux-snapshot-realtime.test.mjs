import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'

const source = await readFile(new URL('../stores/chatRealtime.js', import.meta.url), 'utf8')

test('Linux snapshot mode subscribes, refreshes on reconnect and changes, and keeps decrypted source', async () => {
  const responses = []
  let stream
  class EventSource {
    constructor(url) { this.url = url; stream = this }
    close() { this.readyState = 2 }
  }
  const context = vm.createContext({
    defineStore: (_name, setup) => setup, ref: (value) => ({ value }),
    process: { client: true }, window: {},
    document: { visibilityState: 'visible', addEventListener() {} },
    watch() {}, setTimeout, clearTimeout, EventSource, showErrorAlert() {},
    useApiBase: () => '/analysis/api',
    useChatAccountsStore: () => ({ selectedAccount: 'fixture', ensureLoaded: async () => {},
      applySourceResponse: (response) => responses.push(response) }),
    useApi: () => ({ getChatRealtimeStatus: async () => ({ available: true,
      realtime: { mode: 'linux_snapshot', snapshot_preferred: true } }) }),
  })
  const executable = source.replace(/^import .*$/gm, '')
    .replace('export const useChatRealtimeStore', 'const useChatRealtimeStore')
    .concat('\nthis.store = useChatRealtimeStore()')
  vm.runInContext(executable, context)
  const store = context.store
  assert.equal(await store.enable({ silent: true }), true)
  assert.equal(responses.at(-1).dataSourceStatus.preferredSource, 'decrypted')
  assert.equal(responses.at(-1).dataSourceStatus.fallbackActive, false)
  assert.ok(stream.url.startsWith('/analysis/api/chat/realtime/messages?'))
  stream.onmessage({ data: JSON.stringify({ type: 'ready', mode: 'linux_snapshot' }) })
  assert.equal(store.changeSeq.value, 1)
  assert.equal(store.messageEvent.value.type, 'snapshot_updated')
  stream.onmessage({ data: JSON.stringify({ type: 'snapshot_updated', revision: 2 }) })
  assert.equal(store.changeSeq.value, 2)
  assert.equal(store.messageEventSeq.value, 0)
  await store.disable({ silent: true })
  assert.equal(stream.readyState, 2)
})
