export const useAdvancedApi = () => {
  const base = useApiBase()
  return async (path, options = {}) => {
    try { return await $fetch(base + '/advanced' + path, { timeout: 95000, ...options }) }
    catch (error) { throw new Error(error?.data?.detail || error?.message || '请求失败，请检查服务状态') }
  }
}
