# ✈️ 懶人行程規劃（原型）

睇 IG／YouTube／小紅書旅遊片，又懶得逐間搜尋、自己排行程？
上傳**截圖**，或者貼**店名／Google Maps 連結**，AI（Google Gemini）幫你：

1. **認地方**：上傳最多 10 張截圖會**自動分析**（唔使再撳一次）；亦可以一行一個貼店名或 Maps 連結，再撳「認地方」
2. **地點卡**：原文名、中文名、類別、地區、**地址**、**營業時間**、**評分**、備註、信心程度。基本核對唔使離開呢版。Google Maps 連結只係導航先開
3. **排行程**：地點預設全選、日數預設 3、目的地預設**日本**（可改其他城市或國家）。AI 按地區分日
4. **下載**：行程 Markdown 會帶對得返嘅地址、營業時間、評分

## QA 手動步數（快樂路徑）

**2 步**，由輸入到見到行程：

1. 上傳截圖（自動列出地點），**或者**貼店名／Maps 連結後撳「認地方」
2. 撳「生成行程」（預設日本、3 日、地點全選）

地址、營業時間、評分喺地點卡同行程入面。唔使為咗呢三樣跳出 Google Maps。

Instagram／YouTube／小紅書**連結**未有穩定擷取（避免半套爬蟲）。請上傳該頁截圖，或者貼店名／完整 Maps 連結。`maps.app.goo.gl` 短連結會試吓解析，只會跟去 Google。

## 注意
- 呢個係原型：每個 browser session 有使用次數上限（重新整理會重置；正式版要登入先做到真正 per-user 限額）
- **成功**嘅分析截圖、認地方、生成行程先計 1 次。失敗唔計。同一次入面嘅 HTTP 重試、空回覆重試、分批請求唔另計
- 截圖只會用嚟即時分析，你手機其他資料唔會被接觸
- 多過 3 張截圖會分批同時送去 Gemini，單次請求有逾時。部分批次失敗會先列出其餘地方
- 地址、營業時間、評分：截圖有寫，或者模型有把握先填。未有就係對唔到，唔係叫你去 Maps 先睇到。出發前如果要導航，再用 Maps 連結

## 部署設定（Streamlit Secrets）
| Key | 必填 | 預設 | 用途 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | – | Gemini API key |
| `GEMINI_MODEL` | | `gemini-3.8-flash` | 用邊個 Gemini 模型 |
| `MAX_ANALYSES_PER_SESSION` | | `10` | 每個 session 最多幾多次**成功** AI 動作 |
| `GEMINI_TIMEOUT_MS` | | `90000` | 單次 Gemini HTTP 逾時（毫秒） |
| `GEMINI_RETRY_ATTEMPTS` | | `2` | HTTP 嘗試次數（包含第一次；2 = 逾時／429／5xx 再試一次） |

## 本地開發
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
mkdir -p .streamlit && echo 'GEMINI_API_KEY = "..."' > .streamlit/secrets.toml
streamlit run app.py
python -m pytest -q tests   # 用 mock，唔使真 API key
```

## 檔案
- `app.py` — Streamlit 入口同 UI
- `gemini_client.py` — Gemini（`google-genai` SDK）呼叫、prompt、JSON schema、逾時／重試／分批
- `trip_core.py` — 圖片縮細、JSON 解析、去重、Google Maps 連結、貼上文字、行程 Markdown
- `tests/` — 單元測試同 AppTest
