<template>
  <div style="padding:16px">
    <h1>物主一览</h1>
    <div v-for="i in rows" :key="i.id" class="item">{{ i.owner || '（空）' }} · {{ i.title }} · {{ i.status }}</div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
onMounted(async () => { rows.value = await api('/items') })
</script>
