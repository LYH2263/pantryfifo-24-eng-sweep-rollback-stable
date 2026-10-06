import { reactive } from 'vue'

// Bumped once per "settled" shelf mutation (e.g. an expire-sweep response,
// success OR rollback). Every view that renders the shelf — 全层 / 层页 /
// 顶条 — watches this and refetches in the same tick, so the three can
// never straddle a pre/post-commit boundary.
export const shelfVersion = reactive({ n: 0 })

export function settleShelf() { shelfVersion.n += 1 }
