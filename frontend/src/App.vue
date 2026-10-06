<template>
  <div>
    <div class="alert-bar" v-if="alerts.length">临期预警：{{ alerts.map(a => a.name + '(' + a.level + ')').join(' · ') }}</div>
    <div class="alert-bar" v-else>临期预警带：暂无紧急批次</div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { api } from './api'
import { shelfVersion } from './store'
const alerts = ref([])
async function load() {
  try { alerts.value = await api('/alerts') } catch { alerts.value = [] }
}
// Reload in the same settle tick as 全层/层页 — the bar can never stay
// showing a batch the shelf pages no longer show (or vice versa).
watch(() => shelfVersion.n, load)
onMounted(load)
</script>
