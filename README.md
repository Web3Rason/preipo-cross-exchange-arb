# PreIPO 跨交易所套利監控

針對各交易所上架的 PreIPO 映射股票 / 合約做跨所套利偵測。**25 個合約跨 9 家交易所、7 個 underlying 全 LIVE WS 推送**。

## 快速啟動

需要 Python 3.10+ 與 Node 18+。

```bash
# 1. 後端依賴
pip install -r backend/requirements.txt

# 2. 前端 build（dist/ 不進版控，一定要自己 build，否則首頁會是 404）
cd frontend && npm ci && npm run build && cd ..

# 3. 啟動
start.bat          # 或直接 python backend/main.py
```

開啟 [http://localhost:5032](http://localhost:5032)（前端與 API 同 port，由 FastAPI 直接 serve `frontend/dist`）。

啟動失敗請看 `logs/backend.log`——`start.bat` 是背景啟動，畫面上不會顯示錯誤。

### 開發模式

要改前端的話，另開一個 Vite dev server（`:5173`，`/api` 與 `/ws` 自動 proxy 到後端 5032）：

```bash
python backend/main.py       # 後端
cd frontend && npm run dev   # 前端，改 .vue 立即生效
```

## 問題定義

各交易所對同一個 PreIPO 標的（如 SPACEX）的合約規格不一致：

- **每張合約的定價基準完全不同**：有的是每股美元、有的是估值 (USD/billion)、有的是 SPV mirror token、有的是 IPO Prime 訂閱代幣
- 同一交易所可能有多個變體（HL 同時有 Trade.xyz 的 `xyz:SPCX` + Ventuals 的 `vntl:SPACEX`、BingX 同時有 V2/P2 兩個來源）
- 命名與標籤不統一

→ 所有報價必須先換算成「公司隱含總估值」與「每股隱含價」，才能比較。

## 系統現狀（2026-05-21）

### 9 家交易所、25 個合約全 LIVE

| 交易所 | Connector | 連線方式 | 含 funding | 標的 |
|--------|-----------|---------|-----------|------|
| **Hyperliquid xyz** (Trade.xyz HIP-3) | `hyperliquid_source.py` | WS l2Book + activeAssetCtx | ✓ | SPACEX |
| **Hyperliquid vntl** (Ventuals HIP-3) | 同上 | 同上 | ✓ | SPACEX, OPENAI, ANTHROPIC |
| **OKX** | `okx_source.py` | WS tickers + mark-price + funding-rate | ✓ (固定 0，PreIPO 設計) | SPACEX, OPENAI, ANTHROPIC |
| **Aster** | `aster_source.py` | WS 雙連線（markPrice array + bookTicker）| ✓ | SPACEX |
| **BingX** | `bingx_source.py` | WS bookTicker/ticker/markPrice + REST premiumIndex (funding) | ✓ | SPACEX×2 (V2+P2), OPENAI |
| **Gate.io** | `gate_source.py` | WS futures.book_ticker + futures.tickers | ✓ | SPACEX, OPENAI, ANTHROPIC |
| **Ourbit** | `ourbit_source.py` | WS sub.ticker | ✓ | SPACEX |
| **Bitget** | `bitget_source.py` | WS v2 ticker (SPOT) | spot 無 funding | preSPAX, preOPAI |
| **Binance** | `binance_source.py` | WS 雙連線（markPrice array + bookTicker）| ✓ (TRADIFI 固定 0) | SPACEX |
| **PreStocks** (Solana) | `prestocks_source.py` | WS accountSubscribe + event-driven DexScreener fetch | spot 無 funding | 7 個標的 |

跳過：BitMart（無 PreIPO）、Binance Web3 Wallet（鏈上 SPV、非主站）、Jarsy（需 KYC 無 WS）、KuCoin/CoinW/DeepCoin/Bybit/MEXC（未上架）

### 7 個 underlying

SPACEX (11 合約)、OPENAI (6)、ANTHROPIC (4)、ANDURIL (1)、NEURALINK (1)、KALSHI (1)、POLYMARKET (1)

### 5 種 pricing_basis（normalizer.py 統一換算）

| basis | 公式 | 用於 |
|-------|------|------|
| `per_share_usd` | `valuation = raw × multiplier × total_supply` | xyz:SPCX, Aster, Ourbit, Binance |
| `valuation_billion_usd` | `valuation = raw × 1e9` | vntl:SPACEX, vntl:OPENAI, vntl:ANTHROPIC |
| `valuation_per_billion_usd` | `valuation = raw × 1e9 × multiplier` | OKX, Gate, BingX V2/P2, PreStocks |
| `spot_token_per_share` | `per_share = raw / multiplier` | (歷史保留，目前無使用) |
| `subscription_proxy` | `valuation = raw / sub_price × initial_val` | Bitget preSPAX/preOPAI |

### 3 種 source_family（按底層市場分組做 sanity check）

| family | 涵蓋來源 | 結構性特徵 |
|--------|---------|----------|
| `perp_synth` | xyz, vntl, okx, aster, ourbit, bingx-V2, gateio, binance | 合成永續，跟 IPO 申請估值貼近 |
| `spv_mirror` | prestocks, bingx-P2 | SPV 鏡像，系統性折價 60-70%（SPV 法律風險 + 鏈上流動性淺）|
| `ipo_prime` | bitget | IPO Prime 訂閱式，估值與訂閱錨定 |

**Sanity Check 邏輯**：偏離 _**同 family**_ 中位數 > 50% 才標 outlier。跨 family 差距視為**真實市場結構性訊號保留**（不誤判為計算錯誤）。

## 連線規則（全域）

- **WS-only**：能用 WebSocket 訂閱即時數據（ticker / mark / funding）一律用 WS，**禁止 REST 輪詢**
- **REST 例外**：只允許用於啟動時的一次性 metadata（合約規格 / mint metadata）
- **已知破例**：
  - **PreStocks**：Birdeye WS 需 Business plan、Helius accountSubscribe 自寫 Meteora layout 解析過於複雜 → 採「WS accountSubscribe 訂閱 LB_PAIR 變動 + 事件觸發 DexScreener REST fetch」事件驅動模式（非定時輪詢）
  - **BingX funding**：實測 BingX WS `@premiumIndex` / `@fundingRate` 都回 `code=80015 不支援` → REST `/premiumIndex` 30 秒輪詢（funding 8 小時才結算，30s 遠超必要頻率）

## 架構

```
5032_PreIPO/
├── start.bat                          # 啟動入口
├── launcher.py                        # subprocess.Popen detach backend
├── backend/
│   ├── main.py                        # FastAPI + WS endpoint + serve frontend dist
│   ├── models.py                      # NormalizedMarket dataclass
│   ├── normalizer.py                  # 5 種 pricing_basis 換算公式
│   ├── aggregator.py                  # 串接 9 個 source + family-based sanity check
│   ├── data/preipo_assets.json        # 標的與合約 mapping
│   ├── {exchange}_source.py × 9       # 各家 WS connector
│   └── scripts/                      # 分析腳本（價差、訊號、LP 建倉價推導）
├── frontend/                          # Vue 3 + Vite，build 後 backend serve
│   ├── src/App.vue                    # 主視圖：按 underlying 分組 + family tag
│   ├── dist/                          # build 產物（backend serve 這個）
│   └── ...
└── scripts/check_delistings.py        # 下架巡查
```

## 開發歷程精華（避免後人踩同樣的雷）

### 1. 不要憑公告猜 symbol，要實測 API

第一輪派 Agent 看新聞反推 symbol（如「BingX 應該叫 SPACEX-USDT」），結果跟實際差很大（真實是 `NCSKSPACEXV2USD-USDT` / `NCSKSPACEXP2USD-USDT`）。**每家 connector 寫之前必須先 curl REST 確認 symbol 真實存在**。

### 2. 跟既有實作相同的 pattern 直接抄

既有的交易所 connector 已經有一套 funding/markPrice/bookTicker 的 WS pattern。重複造輪子最後都會出 bug（Aster funding 用單 symbol stream 失敗、改用 array stream 才 work）。

### 3. pricing_basis 必須來自官方明文，不能憑數量級猜

PreStocks 一開始 Agent 從「token 價 $793 vs HL 每股 $200」推測「1 token = 4 shares」，後來深挖才發現 PreStocks 官方 Twitter 明示「token 價 = 估值/10億 USD」（同 OKX/Gate 制）。沒有 Twitter 那條 → 推測 ratio 會錯得離譜。

### 4. Sanity Check 要按 source_family 分組

第一版把所有來源放一起算中位數，PreStocks $806B vs perp 中位數 $2.4T 偏離 67% 被標 OUTLIER → **誤把真實市場結構性折價當計算錯誤**。第二輪審查提出「對齊現實 anchor」概念，進一步推導出「按 source_family 分組」的正確做法。

### 5. 現貨歷史 K 線必須打「現貨」端點，不能沿用合約端點

歷史 backfill 一開始對 mexc/ourbit/gateio 一律走合約 kline 端點，導致 MEXC 現貨 `SPACEX(PRE)USDT`、Ourbit 現貨 `SPAXUSDT` 查無資料（合約端點沒有這些現貨 symbol），Gate 現貨 `SPCX_USDT` 則抓到「合約 K 線冒充現貨」。修正後各走現貨端點（見 `kline_fetchers.py`），三個踩雷點：

- **MEXC / Ourbit 現貨** 用 v3 spot klines `/api/v3/klines`（Ourbit 是 MEXC 白標，`api.ourbit.com` 同款）。interval enum 沒有 `1h`，**1 小時要傳 `60m`**。
- **MEXC v3 spot klines 分頁**：同時帶 `startTime`+`endTime` 時只回 `[startTime, startTime+limit×interval]` 一個窗口（limit 上限 500），不會往 endTime 收斂，且落在上市前的窗口回空 → **必須用固定窗口寬度滑動**，不能拿「回空就停」當終止條件。
- **Gate 現貨 candlesticks** 回的是 array `[t, quoteVol, close, high, low, open, baseVol, closed]`，**欄位順序跟 futures 的 dict `{t,v,c,h,l,o}` 完全不同**，不能共用 parser。

### 6. 同一家的 spot / perp 歷史端點要分流

Bitget 同時有 spot（IPO Prime preSPAX）與 perp（RWA 永續 SPCXUSDT），歷史 backfill 一開始 bitget 分支只實作 spot、perp 直接 return [] → `bitget//SPCXUSDT/perpetual` 沒歷史、估值對比選不到。修正：`_fetch_dispatch` 對 bitget 按 `settlement` 分流，perp 走 mix `/api/v2/mix/market/history-candles`（帶 `productType=USDT-FUTURES`，granularity `1m`/`1H`，**不是 spot 的 `1min`/`1h`**）。同理 gateio/mexc/ourbit 也都按 settlement 分流（見第 5 點）。

## 真實市場觀察（截至 2026-05-21）

### SPACEX 跨 3 family 中位數
```
perp_synth    median = $2.43T   (7 合約)
ipo_prime     median = $2.02T   (1 合約)
spv_mirror    median = $0.80T   (2 合約)  ← 跟 perp 差 67%，SPV 法律風險折價
```

### 真實 funding 套利訊號
```
Aster SPCXUSDT  funding = +0.36%/8h  年化 +398%
HL xyz:SPCX     funding = -0.001%/8h 年化 -1.1%
→ 空 Aster 多 HL xyz，funding 套利年化 ~400%（前提是 share count 對得齊）
```

