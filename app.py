import streamlit as st
from PIL import Image
import google.generativeai as genai
import io

st.set_page_config(page_title="行程分析工具 (原型)", page_icon="✈️", layout="centered")

st.title("✈️ 行程分析工具 (原型版)")
st.caption("上傳截圖，AI 直接睇圖判斷係邊間店")

st.info("""
**呢個係個人試用原型**  
- 上傳截圖，Gemini 會直接睇圖分析  
- 盡量識別餐廳／景點名稱、地址、營業時間  
- 你手機其他資料唔會被接觸
""")

# 從 Secrets 讀取 API Key
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception:
    st.error("未能讀取 API Key。請確認已喺 Streamlit Secrets 設定。")
    model = None

# ========== 上傳截圖 ==========
uploaded = st.file_uploader("上傳截圖（有餐廳／景點資訊嘅畫面）", type=["png", "jpg", "jpeg"])

if uploaded and st.button("分析截圖", type="primary"):
    if model is None:
        st.error("API Key 未設定，無法分析。")
    else:
        with st.spinner("AI 分析中..."):
            img = Image.open(uploaded)
            st.image(img, caption="你嘅截圖", use_container_width=True)

            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()

            prompt = """
你是一個日本旅行助手。請仔細睇呢張截圖，識別裡面提到嘅餐廳、甜品店、景點或其他地方。

請用繁體中文回覆，盡量包含以下資訊（如果畫面有嘅話）：
1. 店舖／景點名稱（日文同中文）
2. 位置／地址
3. 營業時間（如果有）
4. 其他有用資訊（例如評分、特色）
5. 你判斷嘅信心程度（高／中／低）

如果畫面資訊唔夠，請清楚說明邊部分唔肯定，並建議用家可以點樣再確認。
"""

            try:
                response = model.generate_content([
                    prompt,
                    {"mime_type": "image/png", "data": img_bytes}
                ])
                st.subheader("AI 分析結果")
                st.markdown(response.text)
            except Exception as e:
                st.error(f"分析失敗：{e}")

st.divider()
st.caption("原型版本 | 使用 Gemini 睇圖分析")
