"""Gemini 呼叫（新版 google-genai SDK）。

只負責：組 prompt、設定 structured JSON output、叫 API、交俾 trip_core 解析。
HTTP 逾時同 429／5xx 重試交俾 SDK；空回覆同壞 JSON 喺呢度再試一次。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from google import genai
from google.genai import types

import trip_core

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"
# 行程匣認店名，同排行程前核對地址，都用 lite。測試傳入嘅 model 會覆蓋。
# 排日程仍然用 DEFAULT_MODEL（或 secret GEMINI_MODEL）。
DEFAULT_NAMES_MODEL = "gemini-3.5-flash-lite"
DEFAULT_ENRICH_MODEL = "gemini-3.5-flash-lite"
# 核對係文字請求。單次 HTTP 20 秒、SDK 唔再重試，避免 5xx 疊上共用 client 嘅 120 秒。
# enrich_places 只會喺快失敗嘅 5xx／「唔穩定」時再試一次。
ENRICH_TIMEOUT_MS = 20_000
ENRICH_HTTP_ATTEMPTS = 1
ENRICH_RETRY_SLEEP_S = 1.5
ENRICH_RETRY_MAX_ELAPSED_S = 12.0
ENRICH_INCOMPLETE_WARNING = "核對後仍然缺地址／營業時間／評分"
# 只係 Maps 搜尋偏向。一個地區先傳；多過一個或者唔肯定就唔傳。
_ENRICH_MAPS_BIAS = (
    ("福岡", 33.59, 130.40),
    ("東京", 35.68, 139.76),
    ("大阪", 34.69, 135.50),
    ("京都", 35.01, 135.77),
)
# 單次請求上限（毫秒）。Gemini 3 即使用 low thinking，視覺 JSON 都可以近一分鐘。
# app.py 啟動時用 getattr 後備 120_000，避免舊模組未有呢個常數就崩潰。
# Streamlit secret GEMINI_TIMEOUT_MS 如果設咗，會覆蓋呢個預設。
DEFAULT_TIMEOUT_MS = 120_000
# 包含第一次。2 = 失敗再試一次（429／5xx／逾時／連線中斷）。
# 呢個係共用 client 嘅 HTTP 重試，認店名、核對、排日程都用。
# secret GEMINI_RETRY_ATTEMPTS 仍然生效。認店名嘅 JSON 再解析次數係另外嘅 parse_attempts=1。
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_CHUNK_SIZE = 3
DEFAULT_MAX_WORKERS = 2


class GeminiError(Exception):
    """俾 UI 用嘅錯誤：message 係可以直接顯示俾用家睇嘅友善文字。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AnalysisResult:
    places: list[dict]
    warnings: list[str]


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
                    "address": _S(type=_T.STRING, description="地址。唔肯定就空字串，唔好作門牌"),
                    "opening_hours": _S(type=_T.STRING, description="營業時間。唔肯定就空字串"),
                    "rating": _S(
                        type=_T.STRING,
                        description="評分，例如 4.3/5 或 4.3/5（約1200則）。唔肯定就空字串",
                    ),
                    "notes": _S(type=_T.STRING, description="特色、必食（繁體中文，廣東話口吻）。唔好重複評分"),
                    "confidence": _S(type=_T.STRING, enum=list(trip_core.CONFIDENCE_LEVELS)),
                    "source_images": _S(type=_T.ARRAY, items=_S(type=_T.INTEGER),
                                        description="出現喺第幾張圖（由 1 開始）"),
                    "caption_index": _S(
                        type=_T.INTEGER,
                        description="截圖說明嘅編號，① 或 1. = 1。冇編號就 0",
                    ),
                },
                required=[
                    "name", "name_zh", "category", "area", "address", "opening_hours",
                    "rating", "confidence", "source_images", "caption_index",
                ],
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
PLACES_PROMPT = """你係旅行助手，幫唔想自己搜尋嘅「懶人」整理旅遊截圖。
以下係用家上傳嘅 {n} 張截圖（每張前面有「圖片 #編號」）。多數係 Instagram／YouTube／小紅書畫面。
你只可以讀截圖入面睇到嘅字同畫面，唔好假設可以打開任何連結。
目的地：{destination}
偏好：{preferences}
同行：{party}
用家對呢批截圖嘅備註：{remarks}

請識別餐廳、cafe、甜品、景點、商店或其他值得去嘅地方。
如果備註要求只留某幾個編號（例如「只要圖中 1、4」「圖中1，4」「①④」），只輸出該編號嘅地方，其他編號唔好輸出。
如果備註話唔要或不要某幾個編號，就唔好輸出嗰幾個。
預設目的地係日本。如果畫面明顯係其他國家或城市，照實填，唔好硬套日本。
偏好係美食就優先餐廳同甜品；景點就優先景點；兩邊都有就兩類都要。備註指定嘅編號最優先。

每個地方：
- name：原文名稱（日本地方盡量保留日文）
- name_zh：繁體中文名；冇正式譯名就音譯
- category：restaurant / cafe / dessert / attraction / shop / other
- area：地區同城市。日本例子：「博多, 福岡」「今泉, 福岡」「澀谷, 東京」。見到博多、Hakata、今泉就寫福岡。其他國家就寫實際城市
- address：畫面有就照抄。畫面冇、但你對呢間店嘅公開地址有把握先填；否則空字串。唔好作門牌
- opening_hours：畫面有就照抄。否則只係你有把握先填，唔肯定就空字串
- rating：例如「4.3/5」或「4.3/5（約1200則）」。畫面有就照抄；否則只係你有把握先填公開評分，唔好估，唔肯定就空字串
- notes：繁體中文、廣東話口吻，一句特色或必食。唔好重複評分
- confidence：高 / 中 / 低（你有幾肯定呢個名同位置正確）
- source_images：出現喺邊幾張圖（用上面嘅編號）
- caption_index：截圖說明入面嘅編號。① 或 1. 對應 1，② 對應 2。例如「①鉄板焼 日和」係 1，「④ GRASS FED／GELATERIA SAKURA」係 4。冇編號就填 0
同一個地方只列一次，source_images 填晒所有編號。
完全認唔到就回傳空 list。"""

NAMES_SCHEMA = _S(
    type=_T.OBJECT,
    properties={
        "places": _S(
            type=_T.ARRAY,
            items=_S(
                type=_T.OBJECT,
                properties={
                    "name": _S(type=_T.STRING, description="原文名稱"),
                    "name_zh": _S(type=_T.STRING, description="繁體中文名稱或譯名"),
                    "category": _S(type=_T.STRING, enum=list(trip_core.CATEGORIES)),
                    "area": _S(type=_T.STRING, description="地區／城市，例如 今泉, 福岡"),
                    "confidence": _S(type=_T.STRING, enum=list(trip_core.CONFIDENCE_LEVELS)),
                    "source_images": _S(type=_T.ARRAY, items=_S(type=_T.INTEGER)),
                    "caption_index": _S(type=_T.INTEGER, description="說明編號，冇就 0"),
                },
                required=["name", "name_zh", "category", "area", "confidence", "source_images", "caption_index"],
            ),
        )
    },
    required=["places"],
)

NAMES_PROMPT = """你係旅行助手。以下係 {n} 張旅遊截圖（每張前面有「圖片 #編號」）。
只係快認店名，唔好搵地址、營業時間或者評分。
目的地：{destination}
用家備註：{remarks}

如果備註要求只留某幾個編號（例如「只要圖中 1、4」「①④」），只輸出該編號。
如果備註話唔要某幾個編號，就唔好輸出嗰幾個。
畫面係邊個城市就照實寫，唔好硬套日本。見到博多、Hakata、今泉就寫福岡。
連鎖店如果畫面寫咗幾個城市，area 可以用頓號寫晒，例如「福岡、東京」。

每個地方只填：
- name：原文名稱
- name_zh：繁體中文名
- category：restaurant / cafe / dessert / attraction / shop / other
- area：地區同城市
- confidence：高 / 中 / 低
- source_images：邊幾張圖
- caption_index：① 或 1. = 1，冇編號就 0
完全認唔到就回傳空 list。"""

ITINERARY_PROMPT = """你係旅行規劃師，幫「懶人」將收藏嘅地方排成 {days} 日行程。
目的地：{destination}
偏好：{preferences}
同行：{party}
其他要求：{notes}
如果目的地係「日本」又未指定城市，就按地點清單入面嘅城市分日，唔好假設全部喺東京。
其他國家目的地同樣照辦，唔好硬改成日本。

地點清單（JSON，可能有 address、opening_hours、rating）：
{places_json}

排程必須同時用到：
1. 上面嘅行程要求（目的地、日數、偏好、同行；其他要求入面如果有出發日期都要跟）
2. 每個地方嘅 opening_hours：唔好排喺明顯未開門或休息時段；冇時間就唔好假設 24 小時
3. 距離同地理：同一區、地址相近嘅排同一日、順路走；唔好同一日跨好遠嘅城市（例如福岡同東京）。多個城市就按城市分日
4. 偏好：美食多排餐廳；甜品／咖啡留下午；景點做主線；購物、文化、自然就按偏好加比重
5. 同行：自己一個可以密啲；情侶留食飯時間；朋友可以一齊行；家庭（有小朋友）同長輩同行就少步行、多休息、唔好太早太晚

其他：
- 每日由朝到晚排大約時間（time，例如 09:30），早餐／午餐／晚餐 is_meal=true，優先用清單入面嘅餐廳、cafe、甜品
- 地方太多就揀合偏好嘅重點，太少就留自由時間，唔好硬塞
- place_name 用繁體中文（可附原文），activity 用廣東話口吻一句
- maps_query：原文名 + 地址或地區，方便 Google Maps
- tips：交通（日本就提 JR／地鐵／Suica）、預約、邊幾項要出發前再核實營業時間"""

ENRICH_PROMPT = """你係旅行助手。用 Google Maps 核對下面每個地方嘅真實資料。
預設日本；如果名稱明顯係其他地方，照實填，唔好硬套日本。

輸入（店名同地區，例如 kchiara／キアラ、福岡）：
{seeds_json}

每個地方都要用 Google Maps 查。店存在就一定要填到：
- name 必須沿用輸入嘅名稱，唔好改到對唔返
- name_zh：繁體中文
- category：restaurant / cafe / dessert / attraction / shop / other
- area：沿用輸入地區
- address：Maps 上嘅地址
- opening_hours：Maps 上嘅營業時間
- rating：Maps 上嘅評分，例子「4.3/5」
- notes：廣東話口吻一句
- confidence：高 / 中 / 低
- source_images：空陣列
只有 Maps 搵唔到呢間店，address、opening_hours、rating 先可以空字串。唔好估門牌或分數。

只回傳 JSON。可以用 ```json 圍住。格式：
{{"places":[{{"name":"","name_zh":"","category":"restaurant","area":"","address":"","opening_hours":"","rating":"","notes":"","confidence":"中","source_images":[],"caption_index":0}}]}}
"""


# ---------------------------------------------------------------- client
def make_client(
    api_key: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> genai.Client:
    """逾時（毫秒）同 HTTP 重試。attempts 包含第一次，1 代表唔重試。"""
    attempts = max(1, int(retry_attempts))
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=max(1, int(timeout_ms)),
            retry_options=types.HttpRetryOptions(
                attempts=attempts,
                initial_delay=0.4,
                max_delay=2.0,
                exp_base=2.0,
                jitter=0.1,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        ),
    )


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    status = str(getattr(exc, "status", "") or "").lower()
    if "deadline" in status or "timeout" in status:
        return True
    detail = str(getattr(exc, "message", "") or "").lower()
    return "timed out" in detail or "deadline exceeded" in detail


def _generate_text(
    client,
    model: str,
    contents,
    schema: types.Schema | None,
    temperature: float,
    http_options: types.HttpOptions | None = None,
    tools: list | None = None,
    tool_config: types.ToolConfig | None = None,
) -> str:
    # Gemini 3 預設 thinking 係 medium，視覺 JSON 會超過逾時。
    # 只用 thinking_level；thinking_budget 喺 Gemini 3 會 400。
    # Google Maps tool 同 response_schema 一齊用會 400，所以有 tools 就唔傳 schema。
    config_kwargs = {
        "temperature": temperature,
        "thinking_config": types.ThinkingConfig(thinking_level="low"),
    }
    if tools:
        config_kwargs["tools"] = tools
        if tool_config is not None:
            config_kwargs["tool_config"] = tool_config
    elif schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = schema
    if http_options is not None:
        config_kwargs["http_options"] = http_options
    config = types.GenerateContentConfig(**config_kwargs)
    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:  # noqa: BLE001 - 所有 API 錯誤都轉做友善訊息
        logger.exception("Gemini API call failed (model=%s)", model)
        # HTTP 逾時／429／5xx 已由 SDK retry_options 處理；呢度唔再加倍重試。
        raise GeminiError(_friendly_api_error(exc), retryable=False) from exc
    text = getattr(response, "text", None) or ""
    if not str(text).strip():
        logger.warning("Gemini returned empty text: %r", response)
        raise GeminiError(
            "AI 冇俾到結果，可能張圖內容被過濾咗，試下換第二張截圖。",
            retryable=True,
        )
    return text


def _generate_parsed(
    client,
    model: str,
    contents,
    schema: types.Schema | None,
    temperature: float,
    parser,
    *,
    failure_message: str,
    parse_attempts: int,
    retry_sleep: float,
    http_options: types.HttpOptions | None = None,
    tools: list | None = None,
    tool_config: types.ToolConfig | None = None,
):
    """空回覆或壞 JSON 先再試。HTTP 錯誤唔喺呢層打多次，避免同 SDK 重試疊加。"""
    attempts = max(1, int(parse_attempts))
    last_error: GeminiError | None = None
    for attempt in range(attempts):
        try:
            text = _generate_text(
                client, model, contents, schema, temperature, http_options, tools, tool_config,
            )
        except GeminiError as err:
            last_error = err
            if not err.retryable or attempt >= attempts - 1:
                raise
            logger.info("Retrying Gemini call after empty response (%s/%s)", attempt + 1, attempts)
            _sleep(retry_sleep * (attempt + 1))
            continue
        try:
            return parser(text)
        except ValueError:
            logger.warning("Gemini JSON parse failed (attempt %s/%s)", attempt + 1, attempts)
            last_error = GeminiError(failure_message, retryable=True)
            if attempt >= attempts - 1:
                raise last_error
            _sleep(retry_sleep * (attempt + 1))
    if last_error:
        raise last_error
    raise GeminiError(failure_message)


def _friendly_api_error(exc: Exception) -> str:
    if _is_timeout(exc):
        return "AI 回應太慢（已自動重試）。請少傳幾張圖，或者過一陣再試。"
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


def _places_prompt(n: int, trip: dict) -> str:
    return PLACES_PROMPT.format(
        n=n,
        destination=(trip.get("destination") or "").strip() or "日本",
        preferences=(trip.get("preferences") or "").strip() or "（冇特別偏好）",
        party=(trip.get("party") or "").strip() or "（未指定）",
        remarks=(trip.get("remarks") or "").strip() or "（冇）",
    )


def _names_prompt(n: int, trip: dict) -> str:
    return NAMES_PROMPT.format(
        n=n,
        destination=(trip.get("destination") or "").strip() or "日本",
        remarks=(trip.get("remarks") or "").strip() or "（冇）",
    )


def _chunk_places(
    client, model: str, images: list[bytes], trip: dict, *, parse_attempts: int, retry_sleep: float,
    names_only: bool = False,
) -> list[dict]:
    prompt = _names_prompt(len(images), trip) if names_only else _places_prompt(len(images), trip)
    contents: list = [prompt]
    for idx, data in enumerate(images, start=1):
        contents.append(f"圖片 #{idx}")
        contents.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
    return _generate_parsed(
        client,
        model,
        contents,
        NAMES_SCHEMA if names_only else PLACES_SCHEMA,
        0.1 if names_only else 0.2,
        trip_core.parse_places_response,
        failure_message="AI 回覆格式有問題，請再按一次分析。",
        parse_attempts=parse_attempts,
        retry_sleep=retry_sleep,
    )


def _run_chunk(
    client, model, start: int, chunk: list[bytes], trip: dict, *,
    parse_attempts: int, retry_sleep: float, names_only: bool = False,
):
    try:
        places = _chunk_places(
            client, model, chunk, trip,
            parse_attempts=parse_attempts, retry_sleep=retry_sleep, names_only=names_only,
        )
    except GeminiError as err:
        return [], str(err)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error in screenshot chunk starting at %s", start)
        return [], "分析途中出咗意外，請再試一次。"
    for place in places:
        mapped: list[int] = []
        for idx in place.get("source_images") or []:
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if 1 <= idx <= len(chunk):
                mapped.append(idx + start)
        place["source_images"] = mapped
    return places, None


def _analyze_images(
    client,
    model: str,
    images: list[bytes],
    trip: dict,
    *,
    names_only: bool,
    chunk_size: int,
    max_workers: int,
    parse_attempts: int,
    retry_sleep: float,
) -> AnalysisResult:
    if not images:
        return AnalysisResult([], [])
    size = max(1, int(chunk_size))
    chunks: list[tuple[int, list[bytes]]] = []
    start = 0
    while start < len(images):
        chunks.append((start, images[start:start + size]))
        start += size

    ordered: list[tuple[list[dict], str | None]] = [([], None) for _ in chunks]
    if len(chunks) == 1:
        ordered[0] = _run_chunk(
            client, model, chunks[0][0], chunks[0][1], trip,
            parse_attempts=parse_attempts, retry_sleep=retry_sleep, names_only=names_only,
        )
    else:
        workers = max(1, min(int(max_workers), len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _run_chunk, client, model, offset, chunk, trip,
                    parse_attempts=parse_attempts, retry_sleep=retry_sleep, names_only=names_only,
                ): index
                for index, (offset, chunk) in enumerate(chunks)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()

    places: list[dict] = []
    failures: list[str] = []
    for found, err in ordered:
        places.extend(found)
        if err:
            failures.append(err)
    if not places and failures:
        raise GeminiError(failures[0])
    warnings: list[str] = []
    if failures:
        warnings.append(
            f"有 {len(failures)} 組截圖分析失敗，其餘地方已列出。{failures[0]} 可以撳「再分析」補返。"
        )
    kept, note = trip_core.apply_caption_filter(trip_core.dedupe_places(places), trip.get("remarks") or "")
    if note:
        warnings.append(note)
    return AnalysisResult(kept, warnings)


def analyze_screenshots(
    client,
    model: str,
    images: list[bytes],
    *,
    remarks: str = "",
    destination: str = "",
    preferences: str = "",
    party: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    parse_attempts: int = 2,
    retry_sleep: float = 0.4,
) -> AnalysisResult:
    """分批送截圖（每批最多 chunk_size 張），多批會並行。

    source_images 仍然係成個 list 嘅 1-based 位置，方便外面再對畫面編號。
    部分批次失敗會返回已認到嘅地方同 warnings；全部失敗就 raise GeminiError。
    """
    trip = {
        "remarks": remarks,
        "destination": destination,
        "preferences": preferences,
        "party": party,
    }
    return _analyze_images(
        client, model, images, trip,
        names_only=False,
        chunk_size=chunk_size,
        max_workers=max_workers,
        parse_attempts=parse_attempts,
        retry_sleep=retry_sleep,
    )


def recognize_names(
    client,
    model: str | None,
    images: list[bytes],
    *,
    remarks: str = "",
    destination: str = "",
    preferences: str = "",
    party: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    parse_attempts: int = 1,
    retry_sleep: float = 0.4,
) -> AnalysisResult:
    """行程匣快路徑：只認店名同地區。地址、營業時間、評分留待排行程前先核對。

    未傳 model（空字串或 None）就用 DEFAULT_NAMES_MODEL。測試可以傳別嘅 id。
    parse_attempts 預設 1，避免壞 JSON 再等多一次長請求。
    HTTP 重試仍然跟 client 嘅 retry_attempts（預設 2，secret GEMINI_RETRY_ATTEMPTS 會覆蓋）。
    """
    chosen = (model or "").strip() or DEFAULT_NAMES_MODEL
    trip = {
        "remarks": remarks,
        "destination": destination,
        "preferences": preferences,
        "party": party,
    }
    return _analyze_images(
        client, chosen, images, trip,
        names_only=True,
        chunk_size=chunk_size,
        max_workers=max_workers,
        parse_attempts=parse_attempts,
        retry_sleep=retry_sleep,
    )


def _is_unstable(err: BaseException) -> bool:
    """5xx 或已轉成「AI 服務暫時唔穩定」嘅錯誤。逾時訊息唔算，避免再等滿一次。"""
    if "唔穩定" in str(err):
        return True
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    if isinstance(code, int) and code >= 500:
        return True
    cause = err.__cause__
    if cause is not None and cause is not err:
        return _is_unstable(cause)
    return False


def _enrich_http_options() -> types.HttpOptions:
    """覆蓋呢次請求嘅逾時同 SDK 重試。共用 client 仍然係 120 秒、attempts=2。"""
    return types.HttpOptions(
        timeout=ENRICH_TIMEOUT_MS,
        retry_options=types.HttpRetryOptions(attempts=ENRICH_HTTP_ATTEMPTS),
    )


def _enrich_tool_config(seeds: list[dict]) -> types.ToolConfig | None:
    """地區明確係一個已知城市先偏向 Maps。東京都唔好當成京都。"""
    blob = " ".join((place.get("area") or "") for place in seeds)
    blob = blob.replace("東京都", "東京")
    hits = [(lat, lng) for name, lat, lng in _ENRICH_MAPS_BIAS if name in blob]
    if len(hits) != 1:
        return None
    lat, lng = hits[0]
    return types.ToolConfig(
        retrieval_config=types.RetrievalConfig(
            lat_lng=types.LatLng(latitude=lat, longitude=lng),
        )
    )


def _maps_tool_rejected(err: BaseException) -> bool:
    """lite 模型拒絕 Google Maps tool 時先換 DEFAULT_MODEL。一般 400 唔算。"""
    chunks: list[str] = []
    current: BaseException | None = err
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chunks.append(str(current))
        chunks.append(str(getattr(current, "message", "") or ""))
        current = current.__cause__
    blob = " ".join(chunks).lower()
    if "google_maps" in blob or "google maps" in blob:
        return True
    unsupported = any(token in blob for token in ("not supported", "unsupported", "does not support", "isn't supported"))
    return unsupported and ("tool" in blob or "maps" in blob)


def _place_keys(place: dict) -> set[str]:
    return {k for k in (trip_core._norm_key(place.get("name", "")), trip_core._norm_key(place.get("name_zh", ""))) if k}


def _seeds_still_incomplete(seeds: list[dict], merged: list[dict]) -> bool:
    for seed in seeds:
        keys = _place_keys(seed)
        match = None
        for place in merged:
            if keys and keys & _place_keys(place):
                match = place
                break
        if match is None or not trip_core.place_is_complete(match):
            return True
    return False


def enrich_places(
    client,
    model: str | None,
    seeds: list[dict],
    *,
    parse_attempts: int = 1,
    retry_sleep: float = 0.4,
) -> AnalysisResult:
    """用 Google Maps 補地址、營業時間、評分。模型漏咗嘅店名會保留。

    未傳 model（空字串或 None）就用 DEFAULT_ENRICH_MODEL。測試可以傳別嘅 id。
    lite 拒絕 Maps tool 就改用 DEFAULT_MODEL 再叫一次。
    5xx／「唔穩定」而且第一次喺 ENRICH_RETRY_MAX_ELAPSED_S 內失敗，先瞓 1.5 秒再試一次。
    呢次 HTTP 唔跟 SDK 重試，所以唔會變成 120 秒乘幾次。
    Maps tool 唔同 response_schema 一齊用，JSON 由文字解析（容許 ```json）。
    """
    if not seeds:
        return AnalysisResult([], [])
    chosen = (model or "").strip() or DEFAULT_ENRICH_MODEL
    slim = [
        {k: place.get(k) for k in ("name", "name_zh", "area", "address") if place.get(k)}
        for place in seeds
    ]
    prompt = ENRICH_PROMPT.format(seeds_json=json.dumps(slim, ensure_ascii=False, indent=1))
    maps_tool = types.Tool(google_maps=types.GoogleMaps())
    tool_config = _enrich_tool_config(seeds)
    active = chosen

    def once(model_id: str):
        return _generate_parsed(
            client,
            model_id,
            prompt,
            None,
            0.2,
            trip_core.parse_places_response,
            failure_message="AI 回覆格式有問題，請再按一次認地方。",
            parse_attempts=parse_attempts,
            retry_sleep=retry_sleep,
            http_options=_enrich_http_options(),
            tools=[maps_tool],
            tool_config=tool_config,
        )

    def attempt():
        nonlocal active
        try:
            return once(active)
        except GeminiError as err:
            if active == DEFAULT_ENRICH_MODEL and active != DEFAULT_MODEL and _maps_tool_rejected(err):
                logger.info("Maps tool rejected by %s; retrying enrich on %s", active, DEFAULT_MODEL)
                active = DEFAULT_MODEL
                return once(active)
            raise

    started = time.monotonic()
    try:
        found = attempt()
    except GeminiError as err:
        elapsed = time.monotonic() - started
        if _is_unstable(err) and elapsed <= ENRICH_RETRY_MAX_ELAPSED_S:
            logger.info("Enrich unstable after %.1fs; retrying once", elapsed)
            _sleep(ENRICH_RETRY_SLEEP_S)
            found = attempt()
        else:
            raise
    warnings: list[str] = []
    if not found:
        warnings.append("AI 未有補到資料，已先用你貼上嘅名稱。")
        found = []
    merged = trip_core.dedupe_places(list(seeds) + found)
    if _seeds_still_incomplete(seeds, merged):
        warnings.append(ENRICH_INCOMPLETE_WARNING)
    return AnalysisResult(merged, warnings)


def generate_itinerary(client, model: str, places: list[dict], days: int,
                       destination: str = "", notes: str = "",
                       *, preferences: str = "", party: str = "",
                       parse_attempts: int = 2, retry_sleep: float = 0.4) -> dict:
    slim = [
        {
            k: p.get(k)
            for k in ("name", "name_zh", "category", "area", "address", "opening_hours", "rating")
            if p.get(k)
        }
        for p in places
    ]
    prompt = ITINERARY_PROMPT.format(
        days=days,
        destination=(destination or "").strip() or "日本",
        preferences=(preferences or "").strip() or "（冇特別偏好）",
        party=(party or "").strip() or "（未指定）",
        notes=(notes or "").strip() or "（冇）",
        places_json=json.dumps(slim, ensure_ascii=False, indent=1),
    )
    return _generate_parsed(
        client,
        model,
        prompt,
        ITINERARY_SCHEMA,
        0.4,
        trip_core.parse_itinerary_response,
        failure_message="AI 排行程時格式出錯，請再按一次生成。",
        parse_attempts=parse_attempts,
        retry_sleep=retry_sleep,
    )
