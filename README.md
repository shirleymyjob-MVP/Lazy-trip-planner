# ✈️ 懶人行程規劃（原型）

睇 IG／YouTube 旅遊片截咗一堆圖，又懶得逐間搜尋、自己排行程？
將**截圖**丟上嚟，AI（Google Gemini）幫你：

1. **認地方**：一次上傳最多 10 張截圖，識別餐廳、cafe、甜品、景點、商店（自動去除重複）
2. **整理成卡片**：原文名、中文名、類別、地區、地址、營業時間、備註、信心程度，每個都有 Google Maps 連結
3. **排行程**：剔走唔想去嘅地方、揀日數（可填目的地同要求），AI 按地區分日、排時間同食飯位，每日附 Google Maps 路線
4. **下載**：行程可以下載成 Markdown

適用任何目的地（唔限日本）。

> 暫時只支援**截圖**。直接貼 Instagram／YouTube 連結係之後嘅功能。

## 注意
- 呢個係原型：每個 browser session 有使用次數上限（重新整理會重置；正式版要登入先做到真正 per-user 限額）
- 截圖只會用嚟即時分析，你手機其他資料唔會被接觸
- 地址、營業時間由 AI 估計，出發前請再確認

## 部署設定（Streamlit Secrets）
| Key | 必填 | 預設 | 用途 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | – | Gemini API key |
| `GEMINI_MODEL` | | `gemini-3.8-flash` | 用邊個 Gemini 模型 |
| `MAX_ANALYSES_PER_SESSION` | | `10` | 每個 session 最多幾多次 AI 呼叫（分析截圖同生成行程都計） |

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
- `gemini_client.py` — Gemini（`google-genai` SDK）呼叫、prompt、JSON schema
- `trip_core.py` — 圖片縮細、JSON 解析、去重、Google Maps 連結、行程 Markdown
- `tests/` — 單元測試同 AppTest
