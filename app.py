import streamlit as st
import subprocess
import tempfile
import os
import json
from pathlib import Path
from PIL import Image
import easyocr
from deep_translator import GoogleTranslator

st.set_page_config(page_title="行程分析工具 (原型)", page_icon="✈️", layout="centered")

st.title("✈️ 行程分析工具 (原型版)")
st.caption("貼連結 或 上傳截圖，抽出餐廳／景點，並翻譯日文")

st.info("""
**呢個係個人試用原型**  
- 貼連結：攞基本資訊（描述有寫就有用）  
- 上傳截圖：讀畫面文字（較穩定）  
- 自動翻譯日文成中文  
- 你手機其他資料唔會被接觸
""")

tab1, tab2 = st.tabs(["貼連結", "上傳截圖"])

# ========== Tab 1: 貼連結 ==========
with tab1:
    links_input = st.text_area(
        "貼連結（每行一條）",
        height=100,
        placeholder="https://www.instagram.com/reel/...\nhttps://www.youtube.com/watch?v=..."
    )
    if st.button("分析連結", type="primary"):
        if not links_input.strip():
            st.warning("請先貼連結")
        else:
            links = [line.strip() for line in links_input.strip().split("\n") if line.strip()]
            results = []
            progress = st.progress(0)
            for i, link in enumerate(links):
                progress.progress((i) / len(links))
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        cmd = ["yt-dlp", "--no-warnings", "--skip-download", "--write-info-json",
                               "-o", os.path.join(tmpdir, "%(id)s"), link]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
                        info_files = list(Path(tmpdir).glob("*.info.json"))
                        if info_files:
                            with open(info_files[0], "r", encoding="utf-8") as f:
                                info = json.load(f)
                            desc = info.get("description", "")[:800]
                            results.append({
                                "link": link, "status": "成功",
                                "title": info.get("title", "-"),
                                "uploader": info.get("uploader", "-"),
                                "description": desc
                            })
                        else:
                            results.append({"link": link, "status": "失敗", "error": "可能被擋住"})
                except Exception as e:
                    results.append({"link": link, "status": "錯誤", "error": str(e)})
            progress.progress(1.0)
            st.subheader("連結分析結果")
            for r in results:
                with st.expander(f"{r['status']} — {r['link'][:50]}..."):
                    if r["status"] == "成功":
                        st.write(f"**標題**：{r['title']}")
                        st.write(f"**上傳者**：{r['uploader']}")
                        if r["description"]:
                            st.write("**描述**：")
                            st.text(r["description"])
                            if any(ord(c) > 0x3000 for c in r["description"]):
                                try:
                                    translated = GoogleTranslator(source='auto', target='zh-TW').translate(r["description"][:500])
                                    st.write("**翻譯（中文）**：")
                                    st.text(translated)
                                except:
                                    pass
                    else:
                        st.error(r.get("error", "失敗"))

# ========== Tab 2: 上傳截圖 ==========
with tab2:
    uploaded = st.file_uploader("上傳截圖（有餐廳名／景點名嘅畫面）", type=["png", "jpg", "jpeg"])
    if uploaded and st.button("分析截圖", type="primary"):
        with st.spinner("讀取中..."):
            img = Image.open(uploaded)
            st.image(img, caption="你嘅截圖", use_container_width=True)
            try:
                reader = easyocr.Reader(['ja', 'en', 'ch_tra'], gpu=False)
                result = reader.readtext(img, detail=0)
                text = "\n".join(result)
                st.subheader("OCR 讀到嘅文字")
                st.text(text)
                if text.strip():
                    try:
                        translated = GoogleTranslator(source='auto', target='zh-TW').translate(text[:1000])
                        st.subheader("翻譯（中文）")
                        st.text(translated)
                    except Exception as e:
                        st.warning("翻譯暫時失敗")
            except Exception as e:
                st.error(f"OCR 失敗：{e}")

st.divider()
st.caption("原型版本 | 截圖分析較穩定 | 之後可再加強")
