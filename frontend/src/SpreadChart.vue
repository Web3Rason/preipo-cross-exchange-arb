<script setup>
import { ref, watch, onMounted, onUnmounted, computed } from 'vue'
import { createChart, LineSeries } from 'lightweight-charts'

const props = defineProps({
  underlying: String,
})

const contracts = ref([])
const symA = ref('')
const symB = ref('')
const interval = ref('1m')
const stats = ref(null)
const loading = ref(false)
const hoverVal = ref(null)  // { time, a, b, spread_pct, x, y }
const hoverSp = ref(null)   // { time, a, b, spread_pct, x, y }

const valEl = ref(null)
const spEl = ref(null)
let valChart = null
let spChart = null
let lineA = null
let lineB = null
let lineSp = null
let userZoomed = false  // 用戶手動 zoom 後，polling 不要自動 fitContent

// ─── 送出策略到外部下單服務（選配）───
// 5032 contract_key 格式：'{exchange}/{deployer}/{raw_symbol}'
// 下單服務認得的 exchange id
const EX_TO_EXECUTOR = {
  aster: 'aster-future',          // fstream.asterdex.com（非 aster-pro）
  binance: 'binance-future',
  bingx: 'bingx-future',
  bitget: 'bitget-spot',          // 5032 Bitget 是現貨（preSPAX/preOPAI）
  gateio: 'gateio-future',
  hyperliquid: 'hyperliquid-future',  // HIP-3 deployer 會在 contractKeyToExecutorEx 改寫
  mexc: 'mexc-future',
  okx: 'okx-future',
  ourbit: 'ourbit-future',
  // prestocks 無下單服務對應，按鈕會 disable
}
// 下單服務支援的 Hyperliquid HIP-3 deployer
const HL_HIP3_DEPLOYERS = new Set(['vntl', 'xyz'])

// 由 contract_key 決定送給下單服務的 exchange id：
// 主 universe（無 deployer）→ 'hyperliquid-future'
// HIP-3 deployer 'vntl' / 'xyz' → 'vntl-future' / 'xyz-future'
// 其他 deployer → null（下單服務未支援）
function contractKeyToExecutorEx(parsed) {
  if (parsed.ex !== 'hyperliquid') return EX_TO_EXECUTOR[parsed.ex] || null
  if (!parsed.deployer) return 'hyperliquid-future'
  if (HL_HIP3_DEPLOYERS.has(parsed.deployer)) return `${parsed.deployer}-future`
  return null
}
const connectStatus = ref('idle')  // 'idle' | 'ok' | 'err' | 'busy'

function parseContractKey(k) {
  // 4-part: 'okx//SPACEX-USDT-SWAP/perpetual' → { ex:'okx', deployer:'', raw:'SPACEX-USDT-SWAP', settlement:'perpetual' }
  // 4-part: 'hyperliquid/xyz/xyz:SPCX/perpetual' → { ex:'hyperliquid', deployer:'xyz', raw:'xyz:SPCX', settlement:'perpetual' }
  // 3-part back-compat（剛好沒帶 settlement）: 視為 perpetual
  const parts = k.split('/')
  const KNOWN_SETTLEMENTS = ['perpetual', 'spot', 'spot_solana']
  const last = parts[parts.length - 1]
  let settlement = 'perpetual'
  let rawEnd = parts.length
  if (KNOWN_SETTLEMENTS.includes(last)) {
    settlement = last
    rawEnd = parts.length - 1
  }
  return {
    ex: parts[0] || '',
    deployer: parts[1] || '',
    raw: parts.slice(2, rawEnd).join('/'),
    settlement,
  }
}

// 各家 raw symbol → 下單服務用的 symbol 格式
// 必須對得齊，否則下單服務 reload markets 也找不到
function normalizeExecutorSymbol(ex, deployer, raw) {
  if (!raw) return null
  // Hyperliquid HIP-3 deployer:
  // HIP-3：送 raw 即可（'vntl:SPACEX' 保留 prefix）
  if (ex === 'hyperliquid' && deployer && HL_HIP3_DEPLOYERS.has(deployer)) {
    return raw  // 'vntl:SPACEX' / 'xyz:SPCX'
  }
  // HL 主 universe（無 deployer）：接受 'BTC/USDT' 或 'BTC'
  if (ex === 'hyperliquid') return raw
  // BingX: 'NCSKSPACEXV2USD-USDT' / 'NCSKSPACEXP2USD-USDT'
  if (ex === 'bingx' && raw.endsWith('-USDT')) return raw.slice(0, -5) + '/USDT'
  // OKX:   'SPACEX-USDT-SWAP'
  if (ex === 'okx' && raw.endsWith('-USDT-SWAP')) return raw.slice(0, -10) + '/USDT'
  // Bitget 現貨: 'preSPAX' / 'preOPAI' → 真實 ccxt 是 'PRESPAX/USDT' / 'PREOPAI/USDT'
  if (ex === 'bitget' && /^pre/i.test(raw)) return raw.toUpperCase() + '/USDT'
  // Gate / MEXC / Ourbit: 'SPACEX_USDT' / 'SPCX_USDT' / 'SPCXSTOCK_USDT'
  if (raw.endsWith('_USDT')) return raw.slice(0, -5) + '/USDT'
  // Aster / Binance: 'SPCXUSDT'
  if (raw.endsWith('USDT') && !raw.includes('/')) return raw.slice(0, -4) + '/USDT'
  if (raw.includes('/')) return raw
  return null
}

const canConnect = computed(() => {
  if (!symA.value || !symB.value || symA.value === symB.value) return false
  const a = parseContractKey(symA.value)
  const b = parseContractKey(symB.value)
  if (!contractKeyToExecutorEx(a) || !contractKeyToExecutorEx(b)) return false
  return !!normalizeExecutorSymbol(a.ex, a.deployer, a.raw) && !!normalizeExecutorSymbol(b.ex, b.deployer, b.raw)
})

async function sendToExecutor() {
  if (!canConnect.value) return
  connectStatus.value = 'busy'
  const a = parseContractKey(symA.value)
  const b = parseContractKey(symB.value)
  // 用最新 spread 決定方向：spread > 0 (A>B) → short A, long B；< 0 反之
  const spreadIsPositive = (stats.value?.latest ?? 0) >= 0
  const longSide = spreadIsPositive ? b : a   // 便宜的做多
  const shortSide = spreadIsPositive ? a : b  // 貴的做空
  const longExec = contractKeyToExecutorEx(longSide)
  const shortExec = contractKeyToExecutorEx(shortSide)
  const longSym = normalizeExecutorSymbol(longSide.ex, longSide.deployer, longSide.raw)
  const shortSym = normalizeExecutorSymbol(shortSide.ex, shortSide.deployer, shortSide.raw)
  if (!longExec || !shortExec || !longSym || !shortSym) {
    connectStatus.value = 'err'
    setTimeout(() => { connectStatus.value = 'idle' }, 4000)
    return
  }
  // 觀察倍率：讓下單服務的 raw × mul 兩邊都進到「同一 USD 估值單位」。
  //   spread > 0  →  longSide=B (factorB), shortSide=A (factorA)
  //   spread < 0  →  longSide=A (factorA), shortSide=B (factorB)
  // 取 min(factorL, factorS) 當 K，讓較小那邊保持 mul=1、較大那邊放大。
  const factorL = spreadIsPositive ? stats.value?.factorB : stats.value?.factorA
  const factorS = spreadIsPositive ? stats.value?.factorA : stats.value?.factorB
  let longMul = 1, shortMul = 1
  if (factorL && factorS && factorL > 0 && factorS > 0) {
    const K = Math.min(factorL, factorS)
    longMul = +(factorL / K).toFixed(6)
    shortMul = +(factorS / K).toFixed(6)
  }
  let ok = false
  // 選配：把套利機會送到自己的下單服務。
  // 設 VITE_STRATEGY_ENDPOINT（例如 http://127.0.0.1:3005）才會啟用。
  const executorBase = import.meta.env.VITE_STRATEGY_ENDPOINT
  if (!executorBase) {
    alert('未設定 VITE_STRATEGY_ENDPOINT，無法送出策略')
    return
  }
  try {
    const r = await fetch(`${executorBase}/api/pending-strategy`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        long_ex: longExec,
        short_ex: shortExec,
        long_symbol: longSym,
        short_symbol: shortSym,
        symbol: longSym,  // 向後相容
        long_multiplier: longMul,
        short_multiplier: shortMul,
      }),
    })
    ok = r.ok
  } catch (_) {}
  connectStatus.value = ok ? 'ok' : 'err'
  setTimeout(() => { connectStatus.value = 'idle' }, 4000)
}

async function loadContracts() {
  if (!props.underlying) return
  const r = await fetch(`/api/contracts?underlying=${props.underlying}&interval=${interval.value}`)
  const d = await r.json()
  contracts.value = d.contracts || []
  if (contracts.value.length >= 2) {
    if (!contracts.value.includes(symA.value)) symA.value = contracts.value[0]
    if (!contracts.value.includes(symB.value)) symB.value = contracts.value[1]
  }
}

async function loadSpread() {
  if (!symA.value || !symB.value || symA.value === symB.value) return
  loading.value = true
  try {
    const url = `/api/spread?a=${encodeURIComponent(symA.value)}&b=${encodeURIComponent(symB.value)}&interval=${interval.value}`
    const r = await fetch(url)
    const d = await r.json()
    drawCharts(d.series)
    if (d.series.length) {
      const sps = d.series.map(p => p.spread_pct).filter(v => v != null)
      const last = d.series[d.series.length - 1]
      // factor_i = implied_valuation_i / raw_px_i（將 raw 換算到 USD 估值的係數）
      // 兩邊一致才能讓下單服務的 premium 直接反映「估值價差」
      const factorA = (last.a && last.raw_a) ? (last.a / last.raw_a) : null
      const factorB = (last.b && last.raw_b) ? (last.b / last.raw_b) : null
      stats.value = {
        points: d.points,
        min: Math.min(...sps),
        max: Math.max(...sps),
        avg: sps.reduce((a, b) => a + b, 0) / sps.length,
        latest: sps[sps.length - 1],
        factorA, factorB,
        latestRawA: last.raw_a, latestRawB: last.raw_b,
        latestIvA: last.a, latestIvB: last.b,
      }
    }
  } finally {
    loading.value = false
  }
}

function fmtValuationCompact(v) {
  if (v == null) return ''
  const abs = Math.abs(v)
  if (abs >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T'
  if (abs >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B'
  if (abs >= 1e6) return '$' + (v / 1e6).toFixed(0) + 'M'
  return '$' + v.toFixed(0)
}

function ensureCharts() {
  const commonOpts = {
    layout: { background: { color: '#161b22' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#30363d' },
    rightPriceScale: { borderColor: '#30363d', minimumWidth: 80 },
    crosshair: { mode: 1 },
    localization: { priceFormatter: fmtValuationCompact },
  }
  const valSeriesOpts = {
    lineWidth: 2,
    priceFormat: {
      type: 'custom',
      formatter: fmtValuationCompact,
      minMove: 1e8,  // $100M 精度
    },
  }
  if (!valChart && valEl.value) {
    valChart = createChart(valEl.value, { ...commonOpts, width: valEl.value.clientWidth, height: 280 })
    lineA = valChart.addSeries(LineSeries, { ...valSeriesOpts, color: '#58a6ff', title: 'A' })
    lineB = valChart.addSeries(LineSeries, { ...valSeriesOpts, color: '#f0b90b', title: 'B' })
    valChart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        hoverVal.value = null
        if (spChart && lineSp) spChart.clearCrosshairPosition()
        return
      }
      const a = param.seriesData.get(lineA)
      const b = param.seriesData.get(lineB)
      const sp = (a && b && b.value) ? (a.value - b.value) / b.value * 100 : null
      hoverVal.value = {
        time: param.time,
        a: a ? a.value : null,
        b: b ? b.value : null,
        spread_pct: sp,
        x: param.point.x,
        y: param.point.y,
      }
      if (spChart && lineSp && sp != null) {
        spChart.setCrosshairPosition(sp, param.time, lineSp)
      }
    })
  }
  if (!spChart && spEl.value) {
    // spread chart 用 % 格式
    const spOpts = {
      ...commonOpts,
      localization: { priceFormatter: (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + '%' },
    }
    spChart = createChart(spEl.value, { ...spOpts, width: spEl.value.clientWidth, height: 180 })
    lineSp = spChart.addSeries(LineSeries, {
      color: '#3fb950',
      lineWidth: 2,
      title: 'spread %',
      priceFormat: {
        type: 'custom',
        formatter: (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + '%',
        minMove: 0.01,
      },
    })
    spChart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        hoverSp.value = null
        if (valChart && lineA) valChart.clearCrosshairPosition()
        return
      }
      const sp = param.seriesData.get(lineSp)
      const dataA = lineA ? lineA.data() : []
      const dataB = lineB ? lineB.data() : []
      const findVal = (arr, t) => {
        const f = arr.find(d => d.time === t)
        return f ? f.value : null
      }
      const aVal = findVal(dataA, param.time)
      const bVal = findVal(dataB, param.time)
      hoverSp.value = {
        time: param.time,
        a: aVal,
        b: bVal,
        spread_pct: sp ? sp.value : null,
        x: param.point.x,
        y: param.point.y,
      }
      if (valChart && lineA && aVal != null) {
        valChart.setCrosshairPosition(aVal, param.time, lineA)
      }
    })
    // 兩 chart 時間軸同步 + 偵測使用者 zoom/pan
    let syncing = false
    valChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (!r || syncing) return
      syncing = true
      spChart.timeScale().setVisibleLogicalRange(r)
      syncing = false
      userZoomed = true
    })
    spChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (!r || syncing) return
      syncing = true
      valChart.timeScale().setVisibleLogicalRange(r)
      syncing = false
      userZoomed = true
    })
  }
}

function fmtT(v) {
  if (v == null) return '—'
  if (v >= 1e12) return '$' + (v / 1e12).toFixed(3) + 'T'
  if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B'
  return '$' + v.toFixed(0)
}

function fmtTime(t) {
  const d = new Date(t * 1000)
  return d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
}

// 計算 tooltip 位置 — 避開游標 + 不要超出右邊界
function tipStyle(h) {
  const offsetX = 16
  const offsetY = 16
  const tipWidth = 220
  // 估算 chart container 寬度（後面 CSS 會限制不超出）
  const left = h.x + offsetX
  return {
    left: left + 'px',
    top: (h.y + offsetY) + 'px',
    transform: left > 600 ? 'translateX(calc(-100% - 32px))' : 'none',  // 右半邊翻轉到游標左側
  }
}

function drawCharts(series) {
  ensureCharts()
  if (!valChart || !spChart) return
  const dataA = series.filter(p => p.a != null).map(p => ({ time: Math.floor(p.ts / 1000), value: p.a }))
  const dataB = series.filter(p => p.b != null).map(p => ({ time: Math.floor(p.ts / 1000), value: p.b }))
  const dataSp = series.filter(p => p.spread_pct != null).map(p => ({ time: Math.floor(p.ts / 1000), value: p.spread_pct }))
  lineA.setData(dataA)
  lineB.setData(dataB)
  lineSp.setData(dataSp)
  // 只在用戶尚未手動 zoom 時 fitContent，避免覆蓋拖曳/縮放狀態
  if (!userZoomed) {
    valChart.timeScale().fitContent()
    spChart.timeScale().fitContent()
  }
}

function resetZoom() {
  userZoomed = false
  if (valChart) valChart.timeScale().fitContent()
  if (spChart) spChart.timeScale().fitContent()
}

function shortKey(k) {
  // 'okx//SPACEX-USDT-SWAP/perpetual' → 'okx: SPACEX-USDT-SWAP (perp)'
  // 'gateio//SPCX_USDT/spot' → 'gateio: SPCX_USDT (spot)'
  const p = parseContractKey(k)
  const settleLabel = p.settlement === 'spot' ? ' (spot)'
    : p.settlement === 'spot_solana' ? ' (sol)'
    : p.settlement === 'perpetual' ? ' (perp)'
    : ` (${p.settlement})`
  const base = p.deployer ? `${p.ex}/${p.deployer}: ${p.raw}` : `${p.ex}: ${p.raw}`
  return base + settleLabel
}

let pollTimer = null
function startPoll() {
  stopPoll()
  pollTimer = setInterval(loadSpread, 30000)
}
function stopPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

watch(() => props.underlying, async () => {
  userZoomed = false  // 切換 underlying 重設
  await loadContracts()
  await loadSpread()
}, { immediate: false })

watch([symA, symB, interval], async () => {
  userZoomed = false  // 換 contract 或 interval 重設
  await loadContracts()
  await loadSpread()
})

onMounted(async () => {
  await loadContracts()
  await loadSpread()
  startPoll()
})
onUnmounted(() => {
  stopPoll()
  if (valChart) valChart.remove()
  if (spChart) spChart.remove()
})
</script>

<template>
  <div class="spread-chart">
    <div class="chart-controls">
      <span class="chart-title">{{ underlying }} 跨所估值對比</span>
      <select v-model="symA">
        <option v-for="c in contracts" :key="c" :value="c">{{ shortKey(c) }}</option>
      </select>
      <span class="vs">vs</span>
      <select v-model="symB">
        <option v-for="c in contracts" :key="c" :value="c">{{ shortKey(c) }}</option>
      </select>
      <select v-model="interval">
        <option value="1m">1m</option>
        <option value="1h">1h</option>
      </select>
      <button class="reset-zoom-btn" @click="resetZoom" title="拉回顯示全歷史">⟲ Reset Zoom</button>
      <button
        class="connect-btn"
        :class="connectStatus"
        :disabled="!canConnect || connectStatus === 'busy'"
        @click="sendToExecutor"
        :title="canConnect ? '送出策略到下單服務（依當前 spread 自動分配多空）' : '兩邊任一交易所無下單服務對應（例如 PreStocks）'"
      >
        {{ connectStatus === 'ok' ? '→ 已送出' : connectStatus === 'err' ? '✗ 失敗' : connectStatus === 'busy' ? '…' : '🔗 送出策略' }}
      </button>
      <span v-if="stats" class="chart-stats">
        最新 <strong :style="{ color: stats.latest >= 0 ? '#3fb950' : '#f85149' }">{{ stats.latest >= 0 ? '+' : '' }}{{ stats.latest.toFixed(2) }}%</strong>
        ｜ 平均 {{ stats.avg.toFixed(2) }}%
        ｜ 範圍 {{ stats.min.toFixed(2) }}% ~ {{ stats.max.toFixed(2) }}%
        ｜ {{ stats.points }} 點
      </span>
      <span v-if="loading" class="chart-loading">載入中...</span>
    </div>
    <div class="chart-row">
      <div class="chart-label">隱含估值</div>
      <div ref="valEl" class="chart-canvas chart-canvas-rel">
        <div v-if="hoverVal" class="floating-tip" :style="tipStyle(hoverVal)">
          <div class="tip-time">{{ fmtTime(hoverVal.time) }}</div>
          <div><span class="dot-a"></span>A: <strong>{{ fmtT(hoverVal.a) }}</strong></div>
          <div><span class="dot-b"></span>B: <strong>{{ fmtT(hoverVal.b) }}</strong></div>
          <div v-if="hoverVal.spread_pct != null" :style="{ color: hoverVal.spread_pct >= 0 ? '#3fb950' : '#f85149' }">
            spread <strong>{{ hoverVal.spread_pct >= 0 ? '+' : '' }}{{ hoverVal.spread_pct.toFixed(3) }}%</strong>
          </div>
        </div>
      </div>
    </div>
    <div class="chart-row">
      <div class="chart-label">spread %</div>
      <div ref="spEl" class="chart-canvas chart-canvas-rel">
        <div v-if="hoverSp" class="floating-tip" :style="tipStyle(hoverSp)">
          <div class="tip-time">{{ fmtTime(hoverSp.time) }}</div>
          <div><span class="dot-a"></span>A: <strong>{{ fmtT(hoverSp.a) }}</strong></div>
          <div><span class="dot-b"></span>B: <strong>{{ fmtT(hoverSp.b) }}</strong></div>
          <div v-if="hoverSp.spread_pct != null" :style="{ color: hoverSp.spread_pct >= 0 ? '#3fb950' : '#f85149' }">
            spread <strong>{{ hoverSp.spread_pct >= 0 ? '+' : '' }}{{ hoverSp.spread_pct.toFixed(3) }}%</strong>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spread-chart {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 16px;
}
.chart-controls {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.chart-title {
  font-weight: 600;
  font-size: 14px;
  color: #c9d1d9;
}
.chart-controls select {
  background: #0d1117;
  color: #c9d1d9;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 12px;
  min-width: 80px;
}
.reset-zoom-btn {
  background: #0d1117;
  color: #58a6ff;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}
.reset-zoom-btn:hover { background: rgba(88, 166, 255, 0.12); }
.connect-btn {
  background: #0d1117;
  color: #3fb950;
  border: 1px solid #2ea043;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}
.connect-btn:hover:not(:disabled) { background: rgba(63, 185, 80, 0.12); }
.connect-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.connect-btn.ok { color: #3fb950; border-color: #2ea043; background: rgba(63, 185, 80, 0.16); }
.connect-btn.err { color: #f85149; border-color: #f85149; background: rgba(248, 81, 73, 0.12); }
.connect-btn.busy { color: #d29922; border-color: #d29922; }
.vs { color: #8b949e; font-size: 12px; }
.chart-stats { font-size: 12px; color: #8b949e; }
.chart-loading { color: #d29922; font-size: 12px; }
.chart-canvas-rel {
  position: relative;
}
.floating-tip {
  position: absolute;
  pointer-events: none;
  background: rgba(13, 17, 23, 0.95);
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 11px;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  color: #c9d1d9;
  z-index: 100;
  box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  white-space: nowrap;
  line-height: 1.6;
}
.floating-tip .tip-time { color: #8b949e; margin-bottom: 4px; font-size: 10px; }
.floating-tip strong { font-weight: 600; }
.floating-tip .dot-a, .floating-tip .dot-b {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px;
}
.floating-tip .dot-a { background: #58a6ff; }
.floating-tip .dot-b { background: #f0b90b; }

.chart-row {
  display: flex;
  align-items: stretch;
}
.chart-label {
  writing-mode: vertical-rl;
  text-orientation: mixed;
  font-size: 10px;
  color: #8b949e;
  padding: 8px 4px;
  text-align: center;
}
.chart-canvas {
  flex: 1;
}
</style>
