export const useAdvancedFeaturesDialog = () => {
  const open = useState('advanced-features-dialog-open', () => false)
  const context = useState('advanced-features-context', () => ({}))

  const openDialog = (value) => {
    context.value = value?.feature ? { ...value } : {}
    open.value = false
    return navigateTo({ path: '/advanced', query: context.value.feature ? { feature: context.value.feature } : {} })
  }
  const closeDialog = () => { open.value = false }

  return { open, context, openDialog, closeDialog }
}
