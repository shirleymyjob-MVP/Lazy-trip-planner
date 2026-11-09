# 懶人出行 ✈️

貼上你想去的地方，系統會幫你整理清單並安排每日行程。每個 Google 帳戶的行程分開儲存。

日本優先。只收截圖，不會擷取 Instagram／YouTube／小紅書連結。

1. **收集想去的地方**：貼上或批量上傳。截圖先進入待分析，不會自動呼叫 AI。單張可寫備註，再按「開始分析」。批量上傳不設個別備註，按「開始分析全部待處理」。
2. **兩段分析**：先辨識店名與地區（尚未分析／分析中…／已完成／無法辨識）。接著在背景核實地址、營業時間、評分，狀態為「資料核實中…」。認不出可重試或手動輸入店名。
3. **我的收藏**：已登入會員自己存下來的地點。每張卡標明加入者：自己是藍色「我」，其他顯示名是灰色。可篩選地區或類別，✎ 可改地區（可多選）、類別或移除。
4. **一鍵排行程**：先選出發日與日數。類型可多選。風格單選：平衡型（預設）／網紅打卡型／Chill 型／美食優先。收藏少於 3 個時，會問要不要加入 AI 建議。
5. **結果**：按日切換。每一站有時間、說明、交通與時間、營業時間與評分（沒有就顯示「未核實」），以及「來自收藏」或「AI 建議」。可在 Google 地圖打開當日路線。此原型不提供下載，也沒有分享連結。

收費：**待定**。不會用付費牆擋住排行程。

## 與 mockup 對齊

| 畫面 | 現況 |
|---|---|
| 開場：名稱、說明、四步驟、開始收集、我的行程 | 已做 |
| 收集：先排隊、備註、開始分析、批量一次分析、五種狀態、兩段核實、重試／手動店名／改名 | 已做 |
| 收藏：我／其他顯示名顏色、地區與類別、地址時間評分或未核實、篩選、✎ 多地區 | 已做 |
| 排程：出發日＋日數、類型、四種風格、收藏少於 3 個才問 AI、開始排行程 | 已做 |
| 結果：日頁、交通、營業時間、評分、來源標籤、已按風格安排、Google 地圖、不下載 | 已做 |
| 收費：待定，不擋操作 | 已做 |

## 仍待做

- 沒有共同編輯。`?share=` 會被忽略，也不會再寫入網址。
- 「資料核實中…」沒有秒數 ETA。
- 收藏少於 3 個才詢問 AI，這個門檻尚未跟老闆再調。
- 行程標題優先用模型回傳；模型沒給標題時，才用地區加日數。
- Gemini 錯誤句仍有一部分是廣東話口吻。
- 外觀是白底加淺珊瑚色，不是手機外框的像素級複刻。
- 沒有真實 Gemini 時延實測。Google Maps 核對預設關閉，只有 `GEMINI_USE_MAPS=1` 才會開啟。

## 分析與排程

`recognize_names` 只在按下開始分析後執行，問店名同地區。完成後同一輪用 `enrich_places` 做文字核實（`gemini-3.5-flash-lite`），不另計一次用量。`generate_itinerary` 用同一個 lite 模型，單次最多 45 秒。頁尾 `build:` 是目前載入的 commit。

成功的「開始分析」與「開始排行程」各計 1 次。核實地址不另計。失敗不計。

## Google 登入（Phase 1）

未登入的人只會見到登入頁，不會讀取或儲存行程。登入用 Streamlit 內建的 Google OIDC（`st.login` / `st.logout` / `st.user`）。會員身份是 ID token 的 `sub`。行程與圖片都按這個 `sub` 隔離，不會再用 `?share=`。

登入 cookie 名稱是 `_streamlit_user`，有效期 **30 日**（Streamlit 1.66.1 寫死，不能改，也不跟 Google ID token 的 `exp`）。關掉瀏覽器再打開，30 日內會自動還原登入。`st.logout()` 只會登出這個應用程式；Google 沒有 `end_session_endpoint`，Google 帳戶本身仍會保持登入。

`st.login()` 完成、取消或失敗之後，Streamlit 都會 302 回應用程式首頁 `/`，並開啟新 session。

## 行程儲存（Phase 2）

已登入會員的行程寫入 Supabase 資料表 `public.trips`（`payload` jsonb）。圖片寫入私人 Storage bucket `trip-images`，路徑是 `{owner_sub}/{trip_id}/{item_id}.jpg`。伺服器每次操作都用 `auth_gate.get_member_id()` 取出已驗證的 Google `sub`，再核對擁有者。不使用 Supabase Auth。`service_role` 只在伺服器讀取，不會送到瀏覽器。

設定缺失、未登入、行程 id 無效、讀取別人的行程或圖片、或連不上 Supabase 時，畫面顯示錯誤並停止行程讀寫，不會改用本機 `data/trips`，也不會靠 `?share=` 開啟行程。

每位會員預設最多 **3** 個行程、全部行程合計最多 **50** 張圖片。開場頁可以切換或新增行程，直到達到上限。沒有封存、刪除或共同編輯。

同一 Google 帳戶在重新登入或其他裝置上，會讀回自己已儲存的行程和圖片。

Google OAuth 同意畫面必須 **發佈到正式環境（In production）**，不要留在 Testing，否則只有測試用戶名單內的人可以登入。

### `[auth]` 密鑰名稱

只在 `.streamlit/secrets.toml`（本機）或 Streamlit Cloud Secrets 填寫。不要提交真實密鑰。占位示例見 `.streamlit/secrets.toml.example`。

| Key | 說明 |
|---|---|
| `redirect_uri` | 絕對網址，路徑以 `/oauth2callback` 結尾，例如 `https://<app-host>/oauth2callback`。本機預設埠是 `http://localhost:8501/oauth2callback` |
| `cookie_secret` | 自行產生的長隨機字串，用來簽署身份 cookie |
| `client_id` | Google OAuth 用戶端 ID |
| `client_secret` | Google OAuth 用戶端密鑰 |
| `server_metadata_url` | `https://accounts.google.com/.well-known/openid-configuration` |

依賴：`Authlib>=1.3.2`（本專案鎖 `Authlib==1.9.2`）以及 `httpx`（Starlette OIDC client 需要；本專案鎖 `httpx==0.28.1`）。

## 部署設定（Streamlit Secrets）

| Key | 必填 | 預設 | 用途 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | – | Gemini API key |
| `GEMINI_MODEL` | | 不使用 | 認店名、核對、排日程都固定用 `gemini-3.5-flash-lite` |
| `MAX_ANALYSES_PER_SESSION` | | `10` | 每個 session 最多幾次成功的 AI 動作 |
| `GEMINI_TIMEOUT_MS` | | `120000` | 單次 Gemini HTTP 逾時（毫秒） |
| `GEMINI_RETRY_ATTEMPTS` | | `2` | HTTP 嘗試次數（含第一次） |
| `GEMINI_USE_MAPS` | | 關 | 設 `1` 才用 Google Maps 核對 |
| `[supabase] url` | ✅ | – | Supabase 專案 URL，`https://<project>.supabase.co`。頂層 `SUPABASE_URL` 亦可 |
| `[supabase] service_role_key` | ✅ | – | 伺服器專用 service role。頂層 `SUPABASE_SERVICE_ROLE_KEY` 亦可。不要寫進程式或交給瀏覽器 |
| `[limits] max_trips_per_user` | | `3` | 每位會員最多幾個行程。環境變數 `MAX_TRIPS_PER_USER` 亦可 |
| `[limits] max_images_per_user` | | `50` | 每位會員全部行程合計最多幾張圖片。環境變數 `MAX_IMAGES_PER_USER` 亦可 |

Secrets 有值時優先於同名環境變數。測試沒有真實密鑰時，用占位 URL／假 key，並以 mock 客戶端跑測試，不會連接正式專案。

依賴另加 `supabase==2.34.1`（官方 Python 客戶端）。

## 本地開發

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest -q tests
streamlit run app.py
```
