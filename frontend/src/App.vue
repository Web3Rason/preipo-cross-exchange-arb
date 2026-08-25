<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import SpreadChart from './SpreadChart.vue'
import ValuationChart from './ValuationChart.vue'

const selectedUnderlying = ref('SPACEX')
const expandedValuation = ref('')  // 當前展開「總估值走勢」的 underlying（一次只開一個）
function toggleValuationChart(und) {
  expandedValuation.value = expandedValuation.value === und ? '' : und
}
const sortKey = ref('implied_valuation_usd')
const sortOrder = ref('desc')

function toggleSort(key) {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  } else {
    sortKey.value = key
    sortOrder.value = 'desc'
  }
}

function sortArrow(key) {
  if (sortKey.value !== key) return ''
  return sortOrder.value === 'desc' ? ' ▼' : ' ▲'
}

const markets = ref([])
const connected = ref(false)
const lastUpdate = ref(null)
let ws = null
let reconnectTimer = null

const WS_URL = (() => {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
})()

function connectWs() {
  ws = new WebSocket(WS_URL)
  ws.onopen = () => { connected.value = true }
  ws.onclose = () => {
    connected.value = false
    reconnectTimer = setTimeout(connectWs, 2000)
  }
  ws.onerror = () => {}
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'snapshot') {
        markets.value = msg.markets
        lastUpdate.value = new Date()
      }
    } catch (e) {}
  }
}

onMounted(() => connectWs())
onUnmounted(() => {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (ws) ws.close()
})

const grouped = computed(() => {
  const byUnderlying = {}
  for (const m of markets.value) {
    if (!byUnderlying[m.underlying]) {
      byUnderlying[m.underlying] = {
        underlying: m.underlying,
        company_name: m.company_name,
        total_supply_shares: m.total_supply_shares,
        rows: [],
      }
    }
    byUnderlying[m.underlying].rows.push(m)
  }
  for (const g of Object.values(byUnderlying)) {
    g.rows.sort((a, b) => {
      const key = sortKey.value
      let av = a[key], bv = b[key]
      // 字串類用 localeCompare、數字類用減法
      if (typeof av === 'string' || typeof bv === 'string') {
        av = av ?? ''; bv = bv ?? ''
        const cmp = String(av).localeCompare(String(bv))
        return sortOrder.value === 'desc' ? -cmp : cmp
      }
      av = av ?? -Infinity; bv = bv ?? -Infinity
      return sortOrder.value === 'desc' ? bv - av : av - bv
    })
    const live = g.rows.filter(r => r.implied_valuation_usd != null && !r.is_outlier)
    if (live.length >= 2) {
      const sorted = [...live].sort((a, b) => b.implied_valuation_usd - a.implied_valuation_usd)
      const maxR = sorted[0]
      const minR = sorted[sorted.length - 1]
      g.spread_pct = (maxR.implied_valuation_usd - minR.implied_valuation_usd) / minR.implied_valuation_usd * 100
      g.spread_abs_usd = maxR.implied_valuation_usd - minR.implied_valuation_usd
      g.spread_max_label = `${maxR.exchange}${maxR.deployer ? '/' + maxR.deployer : ''}`
      g.spread_min_label = `${minR.exchange}${minR.deployer ? '/' + minR.deployer : ''}`
    } else {
      g.spread_pct = null
    }
    // 按 family 算各自中位數，幫使用者一眼看到跨 family 結構性折價/溢價
    const byFamily = {}
    for (const r of live) {
      if (!byFamily[r.source_family]) byFamily[r.source_family] = []
      byFamily[r.source_family].push(r.implied_valuation_usd)
    }
    g.family_medians = Object.fromEntries(
      Object.entries(byFamily).map(([fam, vals]) => {
        const sorted = [...vals].sort((a, b) => a - b)
        const mid = Math.floor(sorted.length / 2)
        const median = sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
        return [fam, median]
      })
    )
    g.outlier_count = g.rows.filter(r => r.is_outlier).length
  }
  return Object.values(byUnderlying)
})

function fmtUsd(v, digits = 2) {
  if (v == null) return '—'
  return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtPx(v) {
  if (v == null) return '—'
  if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (v >= 1) return v.toFixed(3)
  return v.toFixed(5)
}

function fmtShares(n) {
  if (!n) return '—'
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  return n.toLocaleString()
}

function fmtValuation(v) {
  if (v == null) return '—'
  if (v >= 1e12) return '$' + (v / 1e12).toFixed(3) + 'T'
  if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B'
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M'
  return '$' + v.toFixed(0)
}

function fmtPct(v) {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(3) + '%'
}

function fmtFunding(v) {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(4) + '%/h'
}

function spreadClass(pct) {
  if (pct == null) return 'neutral'
  if (Math.abs(pct) >= 3) return 'warn'
  return ''
}

function exchangeClass(ex) {
  if (ex === 'hyperliquid') return 'hl'
  return ex
}

function fmtFactor(v) {
  if (v == null) return '—'
  if (v >= 100) return v.toFixed(0)
  if (v >= 1) return v.toFixed(4)
  if (v >= 0.01) return v.toFixed(5)
  return v.toExponential(3)
}

function settlementLabel(s) {
  const map = {
    perpetual: '永續合約',
    spot: '現貨',
  }
  return map[s] || s || '—'
}

function familyLabel(f) {
  const map = {
    perp_synth: '合成永續',
    spv_mirror: 'SPV 鏡像',
    ipo_prime: 'IPO Prime',
  }
  return map[f] || f
}

function confLabel(c) {
  const map = { verified: '🟢 官方', partial: '🟡 部分', inferred: '🔴 推測' }
  return map[c] || c
}

function sourceTypeLabel(t) {
  const map = {
    official_announcement: '官方公告',
    official_docs: '官方文檔',
    official_twitter: '官方 Twitter',
    third_party_news: '第三方新聞稿',
    exchange_trade_page: '交易頁',
    exchange_markets_list: '市場列表',
  }
  return map[t] || t
}

function familyColor(f) {
  const map = {
    perp_synth: '#58a6ff',
    spv_mirror: '#d29922',
    ipo_prime: '#a78bfa',
  }
  return map[f] || '#8b949e'
}


const tick = ref(0)
setInterval(() => { tick.value++ }, 1000)

const lastUpdateText = computed(() => {
  void tick.value
  if (!lastUpdate.value) return '尚未收到資料'
  const s = Math.floor((Date.now() - lastUpdate.value.getTime()) / 1000)
  return `${s}s 前`
})
</script>

<template>
  <div class="app">
    <div class="header">
      <h1>5032 PreIPO 跨所套利監控</h1>
      <div class="status">
        <span><span class="dot" :class="{ live: connected }"></span>{{ connected ? 'WS 連線中' : '斷線' }}</span>
        <span>{{ lastUpdateText }}</span>
        <span>{{ markets.length }} 個合約</span>
      </div>
    </div>

    <div class="underlying-tabs">
      <button
        v-for="g in grouped" :key="g.underlying"
        class="underlying-tab"
        :class="{ active: selectedUnderlying === g.underlying }"
        @click="selectedUnderlying = g.underlying"
      >{{ g.underlying }} ({{ g.rows.length }})</button>
    </div>

    <SpreadChart :underlying="selectedUnderlying" />

    <div v-for="g in grouped" :key="g.underlying" class="underlying-group">
      <div class="underlying-header">
        <span class="underlying-name">{{ g.underlying }}</span>
        <span class="underlying-meta">{{ g.company_name }}</span>
        <span class="underlying-supply" :class="{ unknown: !g.total_supply_shares }">
          公司總股本：<strong>{{ g.total_supply_shares ? fmtShares(g.total_supply_shares) + ' 股' : '未公開（無法算每股價）' }}</strong>
        </span>
        <span v-if="g.spread_pct != null" class="underlying-spread" :class="spreadClass(g.spread_pct)"
              :title="`高: ${g.spread_max_label}\n低: ${g.spread_min_label}`">
          跨所最大價差 {{ g.spread_pct.toFixed(2) }}%
          <span class="spread-pair">({{ g.spread_max_label }} ↔ {{ g.spread_min_label }})</span>
        </span>
        <span v-else class="underlying-spread neutral">尚無足夠 live 數據</span>
        <span
          v-for="(med, fam) in g.family_medians" :key="fam"
          class="family-median"
          :style="{ color: familyColor(fam), borderColor: familyColor(fam) + '55' }"
          :title="`${familyLabel(fam)} 系列中位數估值（不同 family 間結構性差距=真實套利訊號）`"
        >
          {{ familyLabel(fam) }} {{ fmtValuation(med) }}
        </span>
        <span v-if="g.outlier_count" class="outlier-summary" title="同 source_family 內偏離中位數 > 50%，多半是 multiplier 設錯，已從跨所價差計算排除">
          ⚠ {{ g.outlier_count }} outlier
        </span>
        <button
          class="valuation-toggle-btn"
          :class="{ active: expandedValuation === g.underlying }"
          @click="toggleValuationChart(g.underlying)"
          :title="expandedValuation === g.underlying ? '點擊收起' : '展開該標的所有合約的歷史總估值線圖'"
        >
          📈 總估值走勢{{ expandedValuation === g.underlying ? ' ▴' : ' ▾' }}
        </button>
      </div>

      <ValuationChart
        v-if="expandedValuation === g.underlying"
        :underlying="g.underlying"
      />

      <table>
        <thead>
          <tr>
            <th class="sortable" @click="toggleSort('exchange')">交易所{{ sortArrow('exchange') }}</th>
            <th class="sortable" @click="toggleSort('raw_symbol')">合約 Symbol{{ sortArrow('raw_symbol') }}</th>
            <th class="sortable" @click="toggleSort('settlement')">合約類型{{ sortArrow('settlement') }}</th>
            <th class="sortable" @click="toggleSort('raw_mark_px')">原始 markPx{{ sortArrow('raw_mark_px') }}</th>
            <th class="sortable" @click="toggleSort('per_share_factor')" title="raw_mark_px × 此因子 = implied_per_share_usd">每股換算因子{{ sortArrow('per_share_factor') }}</th>
            <th class="sortable" @click="toggleSort('implied_per_share_usd')">隱含每股 USD{{ sortArrow('implied_per_share_usd') }}</th>
            <th class="sortable" @click="toggleSort('implied_valuation_usd')">隱含總估值{{ sortArrow('implied_valuation_usd') }}</th>
            <th class="sortable" @click="toggleSort('funding_rate_hourly')">Funding/h{{ sortArrow('funding_rate_hourly') }}</th>
            <th class="sortable" @click="toggleSort('open_interest')">OI{{ sortArrow('open_interest') }}</th>
            <th class="sortable" @click="toggleSort('day_ntl_vlm_usd')">24h 量 (USD){{ sortArrow('day_ntl_vlm_usd') }}</th>
            <th class="sortable" @click="toggleSort('max_leverage')">槓桿{{ sortArrow('max_leverage') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in g.rows" :key="r.exchange + '_' + r.raw_symbol + '_' + (r.deployer || '') + '_' + (r.settlement || '')" :class="{ 'row-outlier': r.is_outlier }">
            <td>
              <span class="exchange-tag" :class="exchangeClass(r.exchange)">{{ r.exchange }}</span>
              <span v-if="r.deployer" class="deployer-tag">{{ r.deployer }}</span>
              <span v-if="r.is_outlier" class="outlier-badge" :title="r.outlier_note">⚠ outlier</span>
            </td>
            <td class="cell-symbol">
              {{ r.raw_symbol }}
            </td>
            <td>
              <span class="settlement-badge" :class="`settle-${r.settlement}`">{{ settlementLabel(r.settlement) }}</span>
            </td>
            <td :class="{ 'cell-no-data': r.raw_mark_px == null }">{{ fmtPx(r.raw_mark_px) }}</td>
            <td :title="r.contract_size != null ? `合約規格 contractSize=${r.contract_size}` : '無 contractSize（spot）'">
              <span v-if="r.per_share_factor != null" class="cell-highlight">×{{ fmtFactor(r.per_share_factor) }}</span>
              <span v-else class="cell-no-data">—</span>
              <span v-if="r.contract_size != null" class="contract-size-sub">cs={{ r.contract_size }}</span>
            </td>
            <td :class="{ 'cell-no-data': r.implied_per_share_usd == null, 'cell-highlight': r.implied_per_share_usd != null && !r.is_outlier, 'cell-outlier': r.is_outlier }">
              {{ r.implied_per_share_usd != null ? '$' + fmtPx(r.implied_per_share_usd) : '—' }}
            </td>
            <td :class="{ 'cell-no-data': r.implied_valuation_usd == null, 'cell-highlight': r.implied_valuation_usd != null && !r.is_outlier, 'cell-outlier': r.is_outlier }" :title="r.outlier_note || r.confidence_note || ''">
              {{ fmtValuation(r.implied_valuation_usd) }}
              <a
                v-if="r.source_url"
                :href="r.source_url"
                target="_blank"
                rel="noopener noreferrer"
                class="confidence-badge confidence-link"
                :class="`conf-${r.confidence}`"
                :title="(r.confidence_note || '') + (r.source_type ? '\n來源類型：' + sourceTypeLabel(r.source_type) : '') + '\n點擊開啟原始來源'"
              >{{ confLabel(r.confidence) }} 🔗</a>
              <span
                v-else
                class="confidence-badge"
                :class="`conf-${r.confidence}`"
                :title="r.confidence_note || ''"
              >{{ confLabel(r.confidence) }}</span>
            </td>
            <td :class="r.funding_rate_hourly > 0 ? 'cell-pos' : (r.funding_rate_hourly < 0 ? 'cell-neg' : '')">
              {{ fmtFunding(r.funding_rate_hourly) }}
            </td>
            <td>{{ r.open_interest != null ? fmtUsd(r.open_interest, 0) : '—' }}</td>
            <td>{{ r.day_ntl_vlm_usd != null ? '$' + fmtUsd(r.day_ntl_vlm_usd, 0) : '—' }}</td>
            <td>{{ r.max_leverage ? r.max_leverage + 'x' : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!markets.length" style="text-align: center; padding: 40px; color: var(--muted);">
      等待後端推送資料...
    </div>
  </div>
</template>
