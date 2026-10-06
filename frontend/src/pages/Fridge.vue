<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot">{{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
      </section>
    </div>
    <button style="margin-top:12px" :disabled="busy" @click="sweep">过期下架</button>
    <p v-if="msg" :class="msg.ok ? 'muted' : 'err'">{{ msg.text }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
const busy = ref(false)
const msg = ref(null)
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() { rows.value = await api('/fridge') }
async function sweep() {
  busy.value = true; msg.value = null
  try {
    const r = await api('/expire-sweep', { method: 'POST', body: '{}' })
    msg.value = { ok: true, text: `已下架 ${r.expired_ids.length} 个批次` }
  } catch (e) {
    // 提交注入失败：后端已整场回滚。明确提示并重载，保证全层/层页/顶条
    // 同时显示提交前状态，绝不留下“顶条已无、全层仍在”的错觉。
    msg.value = { ok: false, text: `下架失败已回滚（无批次离场）：${e.message}` }
  } finally {
    await load()
    window.dispatchEvent(new Event('pantry:changed'))
    busy.value = false
  }
}
onMounted(load)
</script>
