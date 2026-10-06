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
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { api } from './api'
const alerts = ref([])
async function loadAlerts() { try { alerts.value = await api('/alerts') } catch { alerts.value = [] } }
// 下架/消费/入库后由页面派发 pantry:changed；顶条与全层/层页必须同源刷新，
// 避免“顶条已无、全层仍在”这类跨视图不一致（失败回滚后同样会触发重载）。
function onChange() { loadAlerts() }
onMounted(() => { loadAlerts(); window.addEventListener('pantry:changed', onChange) })
onBeforeUnmount(() => window.removeEventListener('pantry:changed', onChange))
</script>
