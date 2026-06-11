"""
HTTP API для кастомной страницы подписки: FreeKassa (СБП/карта), Telegram Stars, CryptoBot.

Защита: заголовок Authorization: Bearer <SUB_PAGE_API_KEY> или X-Sub-Page-Api-Key
(переменная окружения SUB_PAGE_API_KEY в .env).

Публичный каталог тарифов: GET /api/config/tariffs
"""
from __future__ import annotations

import re
import time
from typing import Annotated, Any, Literal, Optional

from aiogram.types import LabeledPrice
from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from bot import bot
from config import (
    ADMIN_IDS,
    BOT_URL,
    CRYPTOBOT_API_TOKEN,
    API_FREEKASSA,
    SHOP_ID_FREEKASSA,
    SUB_PAGE_API_KEY,
    SUB_PAGE_CORS_ORIGINS,
    WEB_API_PORT,
)
from keyboard import keyboard_payment_stars
from lexicon import (
    TARIFF_SAVINGS_PCT,
    dct_desc,
    dct_price,
    lexicon,
    payment_tariff_summary_pro,
    tariff_rub_and_desc,
)
from logging_config import logger
from payments.pay_cryptobot import create_cryptobot_payment
from payments.pay_freekassa import pay as fk_pay
from tariff_resolve import device_from_tariff_key, tariff_days_for_x3

_SUB_PAGE_PAYMENT_SOURCE = "subpage"

DurationId = Literal[
    "m1_d3", "m3_d3", "m6_d3", "m12_d3",
    "m1_d5", "m3_d5", "m6_d5", "m12_d5",
    "m1_d10", "m3_d10", "m6_d10", "m12_d10",
    "white_30",
]

_PRO_TARIFF_RE = re.compile(r"^m\d+_d\d+$")

TARIFF_PUBLIC: list[tuple[str, str, int, bool]] = []
for _devices in (3, 5, 10):
    for _months, _label in ((1, "1 месяц"), (3, "3 месяца"), (6, "6 месяцев"), (12, "12 месяцев")):
        _tid = f"m{_months}_d{_devices}"
        TARIFF_PUBLIC.append((_tid, f"{_label} · {_devices} устройств", _devices, False))

_rate_limits: dict[str, list[float]] = {}


def _rate_check(key: str, max_requests: int, window_sec: int) -> bool:
    now = time.time()
    timestamps = _rate_limits.get(key, [])
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= max_requests:
        _rate_limits[key] = timestamps
        return False
    timestamps.append(now)
    _rate_limits[key] = timestamps
    return True


def _rate_limit_or_raise(request: Request, action: str, max_req: int = 20, window: int = 300) -> None:
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    key = f"{action}:{client_ip}"
    if not _rate_check(key, max_req, window):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много запросов. Подождите несколько минут.",
        )


def _parse_cors_origins(raw: Optional[str]) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _is_pro_tariff_id(tariff_id: str) -> bool:
    return bool(_PRO_TARIFF_RE.fullmatch(tariff_id))


def _site_tariff_price(tariff_id: str) -> Optional[int]:
    if tariff_id == "white_30":
        return int(dct_price.get("white_30", 0))
    if not _is_pro_tariff_id(tariff_id):
        return None
    if tariff_id not in dct_price:
        return None
    return int(dct_price[tariff_id])


def _tariff_parts(tariff_id: str) -> tuple[str, str, bool, int]:
    """desc_key, duration_days_str, white, device_slots."""
    if tariff_id == "white_30":
        return "white_30", str(tariff_days_for_x3("white_30")), True, 1
    if not _is_pro_tariff_id(tariff_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown tariff")
    days = str(tariff_days_for_x3(tariff_id))
    device_n = device_from_tariff_key(tariff_id)
    return tariff_id, days, False, device_n


sub_page_api_key_header = APIKeyHeader(
    name="X-Sub-Page-Api-Key",
    scheme_name="SubPageApiKey",
    auto_error=False,
    description="То же значение, что в .env: SUB_PAGE_API_KEY",
)


async def require_sub_page_auth(
    request: Request,
    x_sub_page_key: Optional[str] = Security(sub_page_api_key_header),
) -> None:
    if not SUB_PAGE_API_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Не задан SUB_PAGE_API_KEY — эндпоинты страницы подписки отключены.",
        )
    if x_sub_page_key == SUB_PAGE_API_KEY:
        return
    bearer = (request.headers.get("Authorization") or "").strip()
    if bearer.lower().startswith("bearer "):
        if bearer[7:].strip() == SUB_PAGE_API_KEY:
            return
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Неверный или отсутствующий ключ. В Swagger нажмите Authorize и введите SUB_PAGE_API_KEY, "
        "или передайте заголовок Authorization: Bearer <ключ>.",
    )


SubPageAuth = Annotated[None, Depends(require_sub_page_auth)]


class SubPagePayIn(BaseModel):
    user_id: int = Field(..., description="Telegram user id")
    duration: DurationId


def _subpage_rub(user_id: int, duration: DurationId) -> int:
    if duration == "white_30":
        rub = int(dct_price["white_30"])
    else:
        rub, _ = tariff_rub_and_desc(duration)
    if user_id in ADMIN_IDS:
        return 1
    return rub


async def _bot_deeplink() -> str:
    if BOT_URL and BOT_URL.strip():
        return BOT_URL.rstrip("/")
    try:
        me = await bot.get_me()
        if me.username:
            return f"https://t.me/{me.username}"
    except Exception as e:
        logger.warning("web_api: не удалось получить username бота: {}", e)
    return "https://t.me/"


app = FastAPI(
    title="Gagarin VPN — API страницы подписки",
    version="2",
    swagger_ui_parameters={"persistAuthorization": True},
)

_cors = _parse_cors_origins(SUB_PAGE_CORS_ORIGINS)
_cors_origins = _cors if _cors else ["*"]
_cors_credentials = bool(_cors)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Sub-Page-Api-Key"],
)


@app.get("/api/config/tariffs")
async def config_tariffs():
    out: list[dict[str, Any]] = []
    for tid, label, devices, first_only in TARIFF_PUBLIC:
        price = _site_tariff_price(tid)
        if price is None:
            continue
        item: dict[str, Any] = {
            "id": tid,
            "label": label,
            "price": price,
            "devices": devices,
        }
        if tid in TARIFF_SAVINGS_PCT:
            item["savings_pct"] = TARIFF_SAVINGS_PCT[tid]
        if first_only:
            item["first_payment_only"] = True
        out.append(item)
    return out


@app.post("/api/v1/sub_page/pay/fk_sbp")
async def sub_page_pay_fk_sbp(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "fk_sbp")
    if not API_FREEKASSA or SHOP_ID_FREEKASSA is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FreeKassa не настроена")

    desc_key, duration_str, white, device_n = _tariff_parts(body.duration)
    rub = _subpage_rub(body.user_id, body.duration)
    des = dct_desc.get(desc_key, f"Gagarin VPN — {duration_str} дней")

    result = await fk_pay(
        val=str(rub),
        des=des,
        user_id=str(body.user_id),
        duration=duration_str,
        white=white,
        ui_kind="sbp",
        source=_SUB_PAGE_PAYMENT_SOURCE,
        device=device_n,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать платёж FreeKassa (СБП)")

    return {
        "payment_url": result.get("url") or "",
        "payment_id": result.get("id") or "",
    }


@app.post("/api/v1/sub_page/pay/fk_card")
async def sub_page_pay_fk_card(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "fk_card")
    if not API_FREEKASSA or SHOP_ID_FREEKASSA is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FreeKassa не настроена")

    desc_key, duration_str, white, device_n = _tariff_parts(body.duration)
    rub = _subpage_rub(body.user_id, body.duration)
    des = dct_desc.get(desc_key, f"Gagarin VPN — {duration_str} дней")

    result = await fk_pay(
        val=str(rub),
        des=des,
        user_id=str(body.user_id),
        duration=duration_str,
        white=white,
        ui_kind="card",
        source=_SUB_PAGE_PAYMENT_SOURCE,
        device=device_n,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать платёж FreeKassa (карта)")

    return {
        "payment_url": result.get("url") or "",
        "payment_id": result.get("id") or "",
    }


@app.post("/api/v1/sub_page/pay/stars")
async def sub_page_pay_stars(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "stars")

    desc_key, duration_str, white, device_n = _tariff_parts(body.duration)
    if body.duration == "white_30":
        stars_amount = int(dct_price.get("white_30", 0))
    else:
        stars_amount = int(dct_price.get(body.duration, 0))
    if body.user_id in ADMIN_IDS:
        stars_amount = 1

    gift_flag = False
    payload = (
        f"user_id:{body.user_id},duration:{duration_str},white:{white},gift:{gift_flag},"
        f"method:stars,amount:{stars_amount},device:{device_n},source:{_SUB_PAGE_PAYMENT_SOURCE}"
    )
    prices = [LabeledPrice(label="XTR", amount=stars_amount)]
    title = f"Оплата подписки на {duration_str} дней."
    if white:
        description = lexicon["payment_link_white"]
    else:
        description = payment_tariff_summary_pro(desc_key)

    try:
        await bot.send_invoice(
            body.user_id,
            title=title,
            description=description,
            prices=prices,
            provider_token="",
            payload=payload,
            currency="XTR",
            reply_markup=keyboard_payment_stars(stars_amount),
        )
    except Exception as e:
        logger.error("web_api stars send_invoice user_id={}: {}", body.user_id, e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Не удалось отправить счёт в Telegram (возможно, бот заблокирован или нет диалога).",
        )

    bot_url = await _bot_deeplink()
    return {"bot_url": bot_url, "stars_amount": stars_amount}


@app.post("/api/v1/sub_page/pay/cryptobot")
async def sub_page_pay_cryptobot(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "cryptobot")
    if not CRYPTOBOT_API_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CryptoBot не настроен")

    desc_key, duration_str, white, device_n = _tariff_parts(body.duration)
    rub = _subpage_rub(body.user_id, body.duration)
    des = dct_desc.get(desc_key, f"Gagarin VPN — {duration_str} дней")

    result = await create_cryptobot_payment(
        rub_amount=rub,
        description=des,
        user_id=body.user_id,
        duration=duration_str,
        white=white,
        is_gift=False,
        source=_SUB_PAGE_PAYMENT_SOURCE,
        device=device_n,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать счёт CryptoBot")

    return {
        "payment_url": result.get("url") or "",
        "invoice_id": result.get("invoice_id"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_api:app", host="0.0.0.0", port=WEB_API_PORT, reload=False)
