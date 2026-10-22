import streamlit as st
import subprocess
import tempfile
import os
import json
from pathlib import Path

st.set_page_config(page_title="行程分析工具 (原型)", page_icon="✈️", layout="centered")

st.title("✈️ 行程分析工具 (原型版)")
st.caption("貼 Instagram / YouTube 連結，嘗試自動抽出餐廳同景點")

st.info("""
**呢個係個人試用原型**  
- 你貼嘅連結會傳去呢個雲端處理（分析完唔會長期儲存）  
- 你手機其他資料唔會被接觸  
- Instagram 有時會擋住，唔係每條都成功
""")

links_input = st.text_area(
    "貼連結（每行一條）",
    height=120,
    placeholder="https://www.instagram.com/reel/...\nhttps://www.youtube.com/watch?v=..."
)

if st.button("分析連結", type="primary"):
    if not links_input.strip():
        st.warning("請先貼連結")
    else:
        links = [line.strip() for line in links_input.strip().split("\n") if line.strip()]
        results = []
        
        progress = st.progress(0)
        status = st.empty()
        
        for i, link in enumerate(links):
            status.write(f"處理中... ({i+1}/{len(links)})")
            progress.progress((i) / len(links))
            
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    cmd = [
                        "yt-dlp",
                        "--no-warnings",
                        "--skip-download",
                        "--write-info-json",
                        "--write-auto-subs",
                        "--sub-langs", "en,ja,zh*",
                        "-o", os.path.join(tmpdir, "%(id)s"),
                        link
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    
                    info_files = list(Path(tmpdir).glob("*.info.json"))
                    if info_files:
                        with open(info_files[0], "r", encoding="utf-8") as f:
                            info = json.load(f)
                        
                        title = info.get("title", "未知標題")
                        description = info.get("description", "")[:500]
                        uploader = info.get("uploader", "")
                        
                        results.append({
                            "link": link,
                            "status": "成功攞到基本資訊",
                            "title": title,
                            "uploader": uploader,
                            "description": description,
                            "note": "完整語音同畫面文字分析需要更多資源，呢個原型暫時顯示基本資訊"
                        })
                    else:
                        results.append({
                            "link": link,
                            "status": "失敗",
                            "error": result.stderr[-300:] if result.stderr else "未知錯誤（可能被 Instagram 擋住）"
                        })
            except Exception as e:
                results.append({
                    "link": link,
                    "status": "錯誤",
                    "error": str(e)
                })
        
        progress.progress(1.0)
        status.write("完成")
        
        st.subheader("分析結果")
        for r in results:
            with st.expander(f"{r['status']} — {r['link'][:60]}..."):
                if r["status"].startswith("成功"):
                    st.write(f"**標題**：{r.get('title', '-')}")
                    st.write(f"**上傳者**：{r.get('uploader', '-')}")
                    if r.get("description"):
                        st.write("**描述片段**：")
                        st.text(r["description"])
                    st.caption(r.get("note", ""))
                else:
                    st.error(r.get("error", "未知錯誤"))
                    st.info("建議：稍後再試，或者先用 YouTube 連結測試")

st.divider()
st.caption("原型版本 | 之後會加：完整抽名、揀選、排行程功能")
