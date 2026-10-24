"""Gemini 呼叫（新版 google-genai SDK）。

只負責：組 prompt、設定 structured JSON output、叫 API、交俾 trip_core 解析。
"""
from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

import trip_core

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"


class GeminiError(Exception):
    """俾 UI 用嘅錯誤：message 係可以直接顯示俾用家睇嘅友善文字。"""


# ---------------------------------------------------------------- schemas
_S = types.Schema
_T = types.Type

PLACES_SCHEMA = _S(
    type=_T.OBJECT,
    properties={
        "places": _S(
            type=_T.ARRAY,
            items=_S(
                type=_T.OBJECT,
                properties={
                    "name": _S(type=_T.STRING, description="原文名稱（例如日文／韓文／英文）"),
                    "name_zh": _S(type=_T.STRING, description="繁體中文名稱或譯名"),
                    "category": _S(type=_T.STRING, enum=list(trip_core.CATEGORIES)),
                    "area": _S(type=_T.STRING, description="地區／城市，例如 澀谷, 東京"),
                    "address": _S(type=_T.STRING),
                    "opening_hours": _S(type=_T.STRING),
                    "notes": _S(type=_T.STRING, description="特色、推介、評分等（繁體中文）"),
                    "confidence": _S(type=_T.STRING, enum=list(trip_core.CONFIDENCE_LEVELS)),
                    "source_images": _S(type=_T.ARRAY, items=_S(type=_T.INTEGER),
                                        description="出現喺第幾張圖（由 1 開始）"),
                },
                required=["name", "name_zh", "category", "area", "confidence", "source_images"],
            ),
        )
    },
    required=["places"],
)

ITINERARY_SCHEMA = _S(
    type=_T.OBJECT,
    properties={
        "title": _S(type=_T.STRING),
        "days": _S(
            type=_T.ARRAY,
            items=_S(
                type=_T.OBJECT,
                properties={
                    "day": _S(type=_T.INTEGER),
                    "area": _S(type=_T.STRING),
                    "theme": _S(type=_T.STRING),
                    "items": _S(
                        type=_T.ARRAY,
                        items=_S(
                            type=_T.OBJECT,
                            properties={
                                "time": _S(type=_T.STRING, description="大約時間，例如 09:30"),
                                "place_name": _S(type=_T.STRING),
                                "activity": _S(type=_T.STRING),
                                "is_meal": _S(type=_T.BOOLEAN),
                                "maps_query": _S(type=_T.STRING,
                                                 description="俾 Google Maps 搜尋用：原文名 + 地址或地區"),
                            },
                            required=["time", "place_name", "activity", "is_meal", "maps_query"],
                        ),
                    ),
                },
                required=["day", "area", "items"],
            ),
        ),
        "tips": _S(type=_T.ARRAY, items=_S(type=_T.STRING)),
    },
    required=["title", "days"],
)

# ---------------------------------------------------------------- prompts
PLACES_PROMPT = """你係一個旅行助手，幫唔想自己搜尋嘅「懶人」整理旅行資料。
以下係用家喺 Instagram / YouTube / 小紅書等旅遊片截落嚟嘅 {n} 張截圖（每張前面有「圖片 #編號」）。
目的地可以係任何國家或城市，唔好預設係日本。

請識別截圖入面提到或者見到嘅餐廳、cafe、甜品店、景點、商店或其他值得去嘅地方：
- name：原文名稱（照畫面寫法，例如日文、韓文、泰文、英文）
- name_zh：繁體中文名稱（冇正式中文名就意譯或音譯）
- category：restaurant / cafe / dessert / attraction / shop / other
- area：地區同城市（例如「澀谷, 東京」「明洞, 首爾」）
- address：畫面有或你好肯定先填，唔肯定就留空
- opening_hours：畫面有或你好肯定先填，唔肯定就留空
- notes：用繁體中文（廣東話口吻）簡短講特色、必食、價錢、評分，或者邊部分唔肯定、建議點樣再確認
- confidence：高 / 中 / 低（你有幾肯定呢個名同位置正確）
- source_images：呢個地方出現喺邊幾張圖（編號）
同一個地方出現喺多張圖，只列一次，source_images 填晒所有編號。
唔好作資料；如果完全認唔到任何地方，回傳空 list。"""

ITINERARY_PROMPT = """你係一個旅行規劃師，幫「懶人」用家將收藏咗嘅地方排成 {days} 日行程。
目的地：{destination}
用家備註：{notes}

地點清單（JSON）：
{places_json}

要求：
- 按地區將就近嘅地方排喺同一日，減少走來走去
- 每日由朝到晚排好大約時間（time，例如 09:30），合理安排早餐／午餐／晚餐（is_meal=true），盡量用清單入面嘅餐廳／cafe／甜品做餐點
- 唔好硬塞：地方太多可以揀重點，太少可以留自由活動時間
- place_name 用繁體中文名（可附原文），activity 用繁體中文、廣東話口吻簡短描述
- maps_query 要方便 Google Maps 搜尋：原文名 + 地址或地區
- tips：幾句實用提示（交通、預約、營業時間要再確認等）"""


# ---------------------------------------------------------------- client
def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def _generate_json(client, model: str, contents, schema: types.Schema, temperature: float) -> str:
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )
    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:  # noqa: BLE001 - 所有 API 錯誤都轉做友善訊息
        logger.exception("Gemini API call failed (model=%s)", model)
        raise GeminiError(_friendly_api_error(exc)) from exc
    text = getattr(response, "text", None)
    if not text:
        logger.warning("Gemini returned empty text: %r", response)
        raise GeminiError("AI 冇俾到結果，可能張圖內容被過濾咗，試下換第二張截圖。")
    return text


def _friendly_api_error(exc: Exception) -> str:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    detail = str(exc)
    if "API_KEY_INVALID" in detail or "API key not valid" in detail:
        code = 401
    if code == 429:
        return "而家用緊嘅人太多（或者用量額度滿咗），請過一陣再試。"
    if code in (401, 403):
        return "AI 服務設定有問題（API Key 無效或冇權限），請通知管理員。"
    if code == 404:
        return "AI 模型設定有問題（搵唔到模型），請通知管理員。"
    if code == 400:
        return "AI 處理唔到呢批圖片，試下少啲張或者換過截圖。"
    if isinstance(code, int) and code >= 500:
        return "AI 服務暫時唔穩定，請過一陣再試。"
    return "連接 AI 服務失敗，請稍後再試。"


def analyze_screenshots(client, model: str, images: list[bytes]) -> list[dict]:
    """一次過（batched）送晒所有截圖，回傳去重後嘅 places。

    images：已經用 trip_core.prepare_image 處理好嘅 JPEG bytes。
    """
    contents: list = [PLACES_PROMPT.format(n=len(images))]
    for idx, data in enumerate(images, start=1):
        contents.append(f"圖片 #{idx}")
        contents.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
    text = _generate_json(client, model, contents, PLACES_SCHEMA, temperature=0.2)
    try:
        return trip_core.parse_places_response(text)
    except ValueError:
        logger.exception("Could not parse places JSON: %s", text[:2000])
        raise GeminiError("AI 回覆格式有問題，請再按一次分析。")


def generate_itinerary(client, model: str, places: list[dict], days: int,
                       destination: str = "", notes: str = "") -> dict:
    slim = [
        {k: p.get(k) for k in ("name", "name_zh", "category", "area", "address", "opening_hours")}
        for p in places
    ]
    prompt = ITINERARY_PROMPT.format(
        days=days,
        destination=destination.strip() or "（由地點推斷）",
        notes=notes.strip() or "（冇）",
        places_json=json.dumps(slim, ensure_ascii=False, indent=1),
    )
    text = _generate_json(client, model, prompt, ITINERARY_SCHEMA, temperature=0.4)
    try:
        return trip_core.parse_itinerary_response(text)
    except ValueError:
        logger.exception("Could not parse itinerary JSON: %s", text[:2000])
        raise GeminiError("AI 排行程時格式出錯，請再按一次生成。")
