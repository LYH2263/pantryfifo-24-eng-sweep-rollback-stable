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
    <button style="margin-top:12px" :disabled="busy" @click="sweep">
      {{ busy ? '提交中…' : '过期下架' }}
    </button>
    <p v-if="message" :class="['muted', rollback ? 'err' : 'ok']">{{ message }}</p>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { api, ApiError } from '../api'
import { shelfVersion, settleShelf } from '../store'
const rows = ref([])
const busy = ref(false)
const message = ref('')
const rollback = ref(false)
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() { rows.value = await api('/fridge') }

async function sweep() {
  if (busy.value) return
  busy.value = true; message.value = ''; rollback.value = false
  try {
    const res = await api('/expire-sweep', { method: 'POST', body: '{}' })
    message.value = `下架完成，批号：${res.expired_ids.join(', ') || '（空）'}`
  } catch (e) {
    if (e instanceof ApiError && e.status === 550) {
      // 提交失败：服务端已整场回滚。不做任何本地猜测，settle 后让全层/
      // 层页/顶条从服务端一起刷新回提交前状态。
      rollback.value = true
      message.value = '提交失败，已整场回滚，页面恢复到提交前状态'
    } else {
      rollback.value = true
      message.value = `下架请求失败：${e.message}（视图以服务端为准刷新）`
    }
  } finally {
    busy.value = false
    // One settle tick regardless of outcome: all shelf views reload together.
    settleShelf()
  }
}

watch(() => shelfVersion.n, load)
onMounted(load)
</script>
