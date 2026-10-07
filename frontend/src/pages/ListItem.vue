<template>
  <div style="padding:16px;max-width:420px">
    <h1>上架</h1>
    <input v-model="title" placeholder="物品名" />
    <input v-model="owner" placeholder="物主" />
    <button @click="go">上架</button>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const title = ref('')
const owner = ref('')
async function go() {
  await api('/items', { method: 'POST', body: JSON.stringify({ title: title.value, owner: owner.value }) })
  router.push('/')
}
</script>
