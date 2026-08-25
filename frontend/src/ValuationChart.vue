<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, LineSeries } from 'lightweight-charts'

const props = defineProps({
  underlying: String,
})

const contracts = ref([])         // contract_key 陣列
const visible = ref({})            // { contract_key: bool } — legend 控制顯示
const interval = ref('1m')
const lookback = ref(168)          // 小時，預設 7 天
const loading = ref(false)
const hover = ref(null)            // { time, x, y, items: [{key,color,value}] }
const pointsTotal = ref(0)

const chartEl = ref(null)
let chart = null
let seriesMap = {}                 // contract_key → ISeriesApi
let userZoomed = false

// 16 色 palette（高對比，深底）
const COLORS = [
  '#58a6ff', '#f0b90b', '#3fb950', '#f85149',
  '#a78bfa', '#39d0d8', '#ff7b72', '#d2a8ff',
  '#fb8500', '#7ce38b', '#ff5e8a', '#56d4dd',
  '#bd7af0', '#ffc66d', '#5ec9ff', '#ff8c42',
]

function colorOf(idx) {
  return COLORS[idx % COLORS.length]
}

function fmtValuationCompact(v) {
  if (v == null) return ''
  const abs = Math.abs(v)
  if (abs >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T'
  if (abs >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B'
  if (abs >= 1e6) return '$' + (v / 1e6).toFixed(0) + 'M'
  return '$' + v.toFixed(0)
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

function parseContractKey(k) {
  const parts = k.split('/')
  const KNOWN = ['perpetual', 'spot', 'spot_solana']
  const last = parts[parts.length - 1]
  let settlement = 'perpetual', rawEnd = parts.length
  if (KNOWN.includes(last)) { settlement = last; rawEnd = parts.length - 1 }
  return {
    ex: parts[0] || '',
    deployer: parts[1] || '',
    raw: parts.slice(2, rawEnd).join('/'),
    settlement,
  }
}

function shortKey(k) {
  const p = parseContractKey(k)
  const settle = p.settlement === 'spot' ? ' (spot)'
    : p.settlement === 'spot_solana' ? ' (sol)'
    : ' (perp)'
  const base = p.deployer ? `${p.ex}/${p.deployer}: ${p.raw}` : `${p.ex}: ${p.raw}`
  return base + settle
}

async function loadContracts() {
  if (!props.underlying) return
  const r = await fetch(`/api/contracts?underlying=${props.underlying}&interval=${interval.value}`)
  const d = await r.json()
  contracts.value = d.contracts || []
  for (const k of contracts.value) {
    if (!(k in visible.value)) visible.value[k] = true
  }
  for (const k of Object.keys(visible.value)) {
    if (!contracts.value.includes(k)) delete visible.value[k]
  }
}

async function loadKlines() {
  if (!contracts.value.length) return
  loading.value = true
  try {
    const results = await Promise.all(contracts.value.map(async (key) => {
      const r = await fetch(`/api/klines?contract_key=${encodeURIComponent(key)}&interval=${interval.value}&lookback_hours=${lookback.value}`)
      const d = await r.json()
      const series = (d.klines || [])
        .filter(p => p.implied_valuation != null && p.implied_valuation > 0)
        .map(p => ({ time: Math.floor(p.ts / 1000), value: p.implied_valuation }))
      return { key, series }
    }))
    drawAll(results)
    pointsTotal.value = results.reduce((s, r) => s + r.series.length, 0)
  } finally {
    loading.value = false
  }
}

function ensureChart() {
  if (chart || !chartEl.value) return
  chart = createChart(chartEl.value, {
    layout: { background: { color: '#161b22' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#30363d' },
    rightPriceScale: { borderColor: '#30363d', minimumWidth: 80 },
    crosshair: { mode: 1 },
    localization: { priceFormatter: fmtValuationCompact },
    width: chartEl.value.clientWidth,
    height: 360,
  })
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point) {
      hover.value = null
      return
    }
    const items = []
    for (const [key, s] of Object.entries(seriesMap)) {
      const d = param.seriesData.get(s)
      if (d) items.push({ key, color: s.options().color, value: d.value })
    }
    items.sort((a, b) => b.value - a.value)
    hover.value = { time: param.time, x: param.point.x, y: param.point.y, items }
  })
  chart.timeScale().subscribeVisibleLogicalRangeChange(() => { userZoomed = true })
}

function drawAll(results) {
  ensureChart()
  if (!chart) return
  // 移除已不存在的合約 series
  const currentKeys = new Set(results.map(r => r.key))
  for (const key of Object.keys(seriesMap)) {
    if (!currentKeys.has(key)) {
      chart.removeSeries(seriesMap[key])
      delete seriesMap[key]
    }
  }
  // 加/更新每個合約
  results.forEach((r, idx) => {
    const color = colorOf(idx)
    if (!seriesMap[r.key]) {
      seriesMap[r.key] = chart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        title: shortKey(r.key),
        priceFormat: { type: 'custom', formatter: fmtValuationCompact, minMove: 1e8 },
        visible: visible.value[r.key] !== false,
      })
    }
    seriesMap[r.key].setData(r.series)
  })
  if (!userZoomed) chart.timeScale().fitContent()
}

function toggleLegend(key) {
  visible.value[key] = !visible.value[key]
  if (seriesMap[key]) {
    seriesMap[key].applyOptions({ visible: visible.value[key] })
  }
}

function resetZoom() {
  userZoomed = false
  if (chart) chart.timeScale().fitContent()
}

function tipStyle(h) {
  const left = h.x + 16
  return {
    left: left + 'px',
    top: (h.y + 16) + 'px',
    transform: left > 600 ? 'translateX(calc(-100% - 32px))' : 'none',
  }
}

let pollTimer = null
function startPoll() {
  stopPoll()
  pollTimer = setInterval(loadKlines, 30000)
}
function stopPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

watch(() => props.underlying, async () => {
  userZoomed = false
  await loadContracts()
  await loadKlines()
})
watch([interval, lookback], async () => {
  userZoomed = false
  await loadContracts()
  await loadKlines()
})

onMounted(async () => {
  await loadContracts()
  await loadKlines()
  startPoll()
})
onUnmounted(() => {
  stopPoll()
  if (chart) chart.remove()
  chart = null
  seriesMap = {}
})
</script>

<template>
  <div class="valuation-chart">
    <div class="chart-controls">
      <span class="chart-title">{{ underlying }} 全合約總估值走勢</span>
      <select v-model="interval">
        <option value="1m">1m</option>
        <option value="1h">1h</option>
      </select>
      <select v-model.number="lookback">
        <option :value="24">24h</option>
        <option :value="168">7d</option>
        <option :value="720">30d</option>
        <option :value="2160">90d</option>
      </select>
      <button class="reset-zoom-btn" @click="resetZoom" title="拉回顯示全歷史">⟲ Reset Zoom</button>
      <span class="chart-stats">{{ contracts.length }} 合約 · {{ pointsTotal }} 點</span>
      <span v-if="loading" class="chart-loading">載入中...</span>
    </div>
    <div class="legend">
      <button
        v-for="(key, idx) in contracts" :key="key"
        class="legend-item"
        :class="{ off: visible[key] === false }"
        @click="toggleLegend(key)"
        :title="key + (visible[key] === false ? ' (已隱藏，點擊顯示)' : ' (點擊隱藏)')"
      >
        <span class="legend-dot" :style="{ background: colorOf(idx) }"></span>
        {{ shortKey(key) }}
      </button>
    </div>
    <div ref="chartEl" class="chart-canvas">
      <div v-if="hover && hover.items.length" class="floating-tip" :style="tipStyle(hover)">
        <div class="tip-time">{{ fmtTime(hover.time) }}</div>
        <div v-for="it in hover.items" :key="it.key" class="tip-row">
          <span class="legend-dot" :style="{ background: it.color }"></span>
          <span class="tip-key">{{ shortKey(it.key) }}</span>
          <strong>{{ fmtT(it.value) }}</strong>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.valuation-chart {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px;
  margin-top: 8px;
  margin-bottom: 16px;
}
.chart-controls {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.chart-title { font-weight: 600; font-size: 14px; color: #c9d1d9; }
.chart-controls select {
  background: #0d1117;
  color: #c9d1d9;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 12px;
  min-width: 70px;
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
.chart-stats { font-size: 12px; color: #8b949e; }
.chart-loading { color: #d29922; font-size: 12px; }
.legend {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.legend-item {
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 11px;
  color: #c9d1d9;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  transition: opacity 0.15s;
}
.legend-item:hover { background: rgba(88, 166, 255, 0.08); }
.legend-item.off { opacity: 0.35; text-decoration: line-through; }
.legend-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
.chart-canvas {
  position: relative;
  min-height: 360px;
}
.floating-tip {
  position: absolute;
  pointer-events: none;
  background: rgba(13, 17, 23, 0.96);
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
  max-height: 480px;
  overflow-y: auto;
}
.floating-tip .tip-time { color: #8b949e; margin-bottom: 4px; font-size: 10px; }
.tip-row {
  display: grid;
  grid-template-columns: 12px 1fr auto;
  gap: 6px;
  align-items: center;
}
.tip-key { color: #8b949e; }
.tip-row strong { font-weight: 600; color: #c9d1d9; }
</style>
