<template>
  <div />
</template>

<script setup>
// Keep old bookmarks working after removing the first-use agreement screen.
definePageMeta({
  middleware: () => navigateTo('/', { replace: true }),
})
</script>
