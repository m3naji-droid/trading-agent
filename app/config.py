"""
الإعدادات العامة للوكيل. كل القيم تُقرأ من ملف .env ويمكن تركها على الافتراضي.
"""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


@dataclass
class Settings:
    # ---- الوضع ----
    # paper   = محاكاة داخلية بأسعار Binance الحقيقية (لا تحتاج مفاتيح)
    # testnet = حساب Binance التجريبي (يحتاج مفاتيح testnet)
    # live    = أموال حقيقية
    mode: str = os.getenv("MODE", "paper").lower()

    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")

    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "change-me")
    paper_start_balance: float = _f("PAPER_START_BALANCE", 300)

    # ---- نطاق العملات ----
    universe_size: int = _i("UNIVERSE_SIZE", 30)          # أعلى N عملة سيولة
    min_quote_volume: float = _f("MIN_QUOTE_VOLUME", 20_000_000)  # حجم تداول يومي أدنى بالدولار
    scan_interval_sec: int = _i("SCAN_INTERVAL_SEC", 60)
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 4)

    # ---- خانة العملات الميمية (قواعد أشد) ----
    memes: tuple = tuple(x.strip().upper() for x in os.getenv(
        "MEMES", "DOGE,SHIB,PEPE,WIF,BONK,FLOKI,PENGU,TRUMP,BOME,NEIRO").split(",") if x.strip())
    meme_slots: int = _i("MEME_SLOTS", 5)
    meme_min_volume: float = _f("MEME_MIN_VOLUME", 10_000_000)
    meme_risk_mult: float = _f("MEME_RISK_MULT", 0.5)     # نصف المخاطرة
    meme_max_leverage: int = _i("MEME_MAX_LEVERAGE", 2)
    meme_max_positions: int = _i("MEME_MAX_POSITIONS", 1)

    # ---- المخاطر ----
    risk_min: float = _f("RISK_MIN", 0.01)            # 1%
    risk_max: float = _f("RISK_MAX", 0.02)            # 2%
    risk_reduced: float = _f("RISK_REDUCED", 0.005)   # 0.5% بعد سلسلة خسائر
    loss_streak_trigger: int = _i("LOSS_STREAK_TRIGGER", 3)
    max_daily_drawdown: float = _f("MAX_DAILY_DRAWDOWN", 0.06)  # 6% => مفتاح الطوارئ
    max_leverage: int = _i("MAX_LEVERAGE", 5)          # سقف صارم لا يتجاوزه الوكيل
    max_total_exposure: float = _f("MAX_TOTAL_EXPOSURE", 3.0)   # مجموع القيمة الاسمية / الرصيد

    # ---- التعلم ----
    pretrain_bars: int = _i("PRETRAIN_BARS", 17520)  # سنتان من شموع الساعة
    memory_similarity: float = _f("MEMORY_SIMILARITY", 0.90)
    min_confidence: float = _f("MIN_CONFIDENCE", 0.55)

    fee_rate: float = _f("FEE_RATE", 0.0005)    # عمولة تقديرية لكل جهة
    data_dir: str = os.getenv("DATA_DIR", "data")

    timeframes: tuple = ("15m", "1h", "4h", "1d")
    entry_tf: str = "15m"
    trend_tf: str = "1h"


settings = Settings()
os.makedirs(settings.data_dir, exist_ok=True)
