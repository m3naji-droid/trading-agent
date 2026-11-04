"""
الوكيل الذكي: الحلقة الرئيسية التي تعمل 24/7.
المسح → التحليل → التعلم → القرار → التنفيذ → الإدارة → التعلم من النتيجة.
"""
import asyncio
import gc
import logging
import time
from types import SimpleNamespace

from .config import settings
from . import memory
from .exchange import make_clients, build_universe, ohlcv_df, check_key_permissions
from .indicators import enrich
from .regime import detect_regime, HIGH_RISK, TREND_UP, TREND_DOWN, LABELS_AR
from . import smc as smc_mod
from .strategies import run_all, apply_orderflow, features, STRATEGY_AR
from .orderflow import LiquidationTracker, snapshot
from .learner import Learner, pretrain, simulate_trade
from .risk import RiskManager
from .broker import PaperBroker, LiveBroker

log = logging.getLogger("agent")
EXIT_AR = {"stop": "وقف الخسارة", "trailing": "الوقف المتحرك", "target": "الهدف", "timeout": "انتهاء المدة",
           "kill": "مفتاح الطوارئ", "regime": "تغيّر حالة السوق", "exchange_stop": "وقف المنصة"}


def memory_usage():
    """استهلاك الذاكرة: الوكيل نفسه + السيرفر كاملاً (بالميغابايت)."""
    out = {"agent_mb": 0, "total_mb": 0, "available_mb": 0}
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                out["agent_mb"] = int(line.split()[1]) // 1024
        info = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        out["total_mb"] = info.get("MemTotal", 0) // 1024
        out["available_mb"] = info.get("MemAvailable", 0) // 1024
        out["swap_mb"] = info.get("SwapTotal", 0) // 1024
    except Exception:
        pass
    return out


class Agent:
    def __init__(self):
        memory.init()
        self.mode = memory.kv_get("mode") or settings.mode
        self.running = memory.kv_get("running", True)
        self.risk = RiskManager()
        self.learner = Learner()
        self.liq = LiquidationTracker()
        self.universe, self.analysis, self.prices = [], {}, {}
        self.rank_info = {}
        self.restart_requested = False
        self.shadow = memory.kv_get("shadow", {})
        self.pretrained = set(memory.kv_get("pretrained", []))
        self.pretrain_status = "pending"
        self.equity = 0.0
        self.heartbeat = time.time()
        self.status = "بدء التشغيل"
        self.market_risk_off = False
        self.key_check = None
        self._last_universe = 0
        self._last_equity_rec = 0
        self.spot = self.fut = self.broker = None

    # ================================================================ التشغيل
    async def start(self):
        if self.mode in ("testnet", "live") and not settings.api_key:
            memory.log_event("⚠️ لا توجد مفاتيح API — التحويل لوضع المحاكاة paper", "warn")
            self.mode = "paper"
        self.spot, self.fut = make_clients(self.mode)
        if self.mode == "paper":
            self.broker = PaperBroker(self.spot, self.fut)
        else:
            self.key_check = await check_key_permissions(self.spot)
            memory.log_event(f"🔐 {self.key_check['msg']}", "info" if self.key_check["ok"] else "error")
            if not self.key_check["ok"]:
                self.risk.freeze("مفتاح API يملك صلاحية السحب", manual=True)
            self.broker = LiveBroker(self.spot, self.fut, self.mode)
        memory.log_event(f"🚀 بدأ الوكيل في وضع {self.mode.upper()}")
        asyncio.create_task(self.liq.run())
        asyncio.create_task(self.loop())

    async def loop(self):
        backoff = 5
        while True:
            t0 = time.time()
            try:
                await self.cycle()
                backoff = 5
                self.status = "يعمل"
            except Exception as e:  # تعافٍ ذاتي: أي خطأ لا يوقف الوكيل
                log.exception("cycle error")
                self.status = f"خطأ مؤقت: {str(e)[:80]}"
                memory.log_event(f"⚠️ خطأ في الدورة: {str(e)[:150]} — إعادة المحاولة بعد {backoff}ث", "warn")
                await asyncio.sleep(backoff)
                backoff = min(300, backoff * 2)
            self.heartbeat = time.time()
            gc.collect()  # تحرير الذاكرة بعد كل دورة
            if self.restart_requested:  # تحديث: إعادة تشغيل بين الدورات (الصفقات محفوظة)
                import os
                await asyncio.sleep(1)
                os._exit(0)
            await asyncio.sleep(max(1, settings.scan_interval_sec - (time.time() - t0)))

    # ================================================================ الدورة
    async def cycle(self):
        if time.time() - self._last_universe > 6 * 3600 or not self.universe:
            uni, self.rank_info, source = await build_universe(
                self.spot, self.fut, settings.universe_size, settings.min_quote_volume)
            if "BTC/USDT" not in uni:
                uni.insert(0, "BTC/USDT")
            added = [s for s in uni if s not in self.universe]
            self.universe = uni
            self._last_universe = time.time()
            names = "، ".join(s.split("/")[0] for s in uni)
            memory.log_event(f"🔭 أفضل {len(uni)} عملة حسب {source}: {names}")
            if added and len(added) < len(uni):
                memory.log_event(f"➕ عملات جديدة دخلت القائمة: {'، '.join(s.split('/')[0] for s in added)}")
            if self.pretrain_status != "running":
                asyncio.create_task(self.pretrain_all())

        watch = sorted(set(self.universe) | {p["symbol"] for p in self.broker.positions.values()})
        tick = await self.spot.fetch_tickers(watch)
        self.prices = {s: float(t["last"]) for s, t in tick.items() if t.get("last")}

        for pos, fill, pnl in await self.broker.sync(self.prices):
            self._on_closed(pos, fill, pnl, "exchange_stop")

        self.equity = await self.broker.equity(self.prices)
        if time.time() - self._last_equity_rec > 300:
            memory.record_equity(self.equity)
            self._last_equity_rec = time.time()
        reason = self.risk.on_equity(self.equity)
        if reason:
            await self.kill(reason, manual=False)

        sem = asyncio.Semaphore(3)  # تحليل 3 عملات بالتوازي فقط لتوفير الذاكرة

        async def run(sym):
            async with sem:
                try:
                    return await self.analyze_symbol(sym)
                except Exception as e:
                    log.debug(f"{sym}: {e}")
                    return []

        results = await asyncio.gather(*[run(s) for s in self.universe])
        btc = self.analysis.get("BTC/USDT", {})
        risk_off = btc.get("regime") == HIGH_RISK
        if risk_off != self.market_risk_off:
            self.market_risk_off = risk_off
            memory.log_event("🛑 BTC في حالة خطورة عالية — إيقاف الدخول وحماية المحفظة" if risk_off
                             else "✅ زال خطر السوق العام — استئناف التحليل", "warn" if risk_off else "info")

        await self.manage_positions()
        await self.decide([c for r in results for c in r])
        memory.kv_set("shadow", self.shadow)

    # ================================================================ التحليل
    async def analyze_symbol(self, sym):
        df15 = await ohlcv_df(self.spot, sym, settings.entry_tf, 300)
        h1 = await ohlcv_df(self.spot, sym, settings.trend_tf, 300)
        if len(df15) < 260 or len(h1) < 220:
            return []
        df15, h1 = df15.iloc[:-1], h1.iloc[:-1]  # الشموع المغلقة فقط

        def compute():
            e15 = enrich(df15).dropna().reset_index(drop=True)
            e1 = enrich(h1).dropna().reset_index(drop=True)
            reg = detect_regime(e1)
            ctx = {"df": e15, "htf": e1, "regime": reg, "smc": smc_mod.analyze(e15), "flow": None}
            return ctx, run_all(ctx)

        ctx, sigs = await asyncio.to_thread(compute)
        self._resolve_shadows(sym, ctx["df"])
        s = ctx["smc"]
        self.analysis[sym] = {
            "symbol": sym, "price": self.prices.get(sym, float(ctx["df"]["close"].iloc[-1])),
            "regime": ctx["regime"]["regime"], "label": ctx["regime"]["label"],
            "adx": round(ctx["regime"]["adx"], 1), "phase": s["phase"], "bos": s["bos"],
            "fvg": len(s["fvgs"]), "ob": len(s["order_blocks"]),
            "rsi": round(float(ctx["df"]["rsi"].iloc[-1]), 1),
            "signals": [x.strategy for x in sigs], "learned": sym in self.pretrained,
            "rank": (self.rank_info.get(sym) or {}).get("rank"),
        }
        return [(sym, ctx, sg) for sg in sigs]

    # ================================================================ القرار
    async def decide(self, cands):
        open_syms = {p["symbol"] for p in self.broker.positions.values()}
        scored = []
        for sym, ctx, sig in cands:
            price = self.prices.get(sym, sig.entry)
            ctx["flow"] = await snapshot(self.spot, self.fut, sym, price, self.liq)
            apply_orderflow(sig, ctx["flow"])
            reg = ctx["regime"]["regime"]
            conf = self.learner.blended_confidence(sig, reg)
            feats = features(ctx, sig)
            allowed, why = memory.check_pattern(sig.strategy, sig.side, feats)
            self._open_shadow(sym, ctx, sig, feats)
            self.analysis[sym]["last_decision"] = {"strategy": STRATEGY_AR[sig.strategy], "side": sig.side,
                                                   "conf": round(conf, 2), "memory": why, "allowed": allowed}
            if not allowed:
                memory.log_event(f"🧠 رفض {sym} {STRATEGY_AR[sig.strategy]}: {why}")
                continue
            if conf >= settings.min_confidence and sym not in open_syms:
                scored.append((conf * min(sig.rr, 3), conf, sym, ctx, sig, feats, why))

        if not self.running or self.risk.frozen or self.market_risk_off or self.pretrain_status == "pending":
            return
        scored.sort(key=lambda x: -x[0])
        for _, conf, sym, ctx, sig, feats, why in scored:
            slots = settings.max_open_positions - len(self.broker.positions)
            if slots <= 0:
                break
            if sym in {p["symbol"] for p in self.broker.positions.values()}:
                continue
            if sym not in self.pretrained:
                continue  # لا يتداول عملة قبل دراسة تاريخها
            await self.open_position(sym, ctx, sig, conf, feats, why, slots)

    async def open_position(self, sym, ctx, sig, conf, feats, why, slots):
        price = self.prices.get(sym, sig.entry)
        # إعادة حساب الوقف والهدف على السعر الحالي بنفس المسافات
        shift = price - sig.entry
        sig.entry, sig.stop, sig.target = price, sig.stop + shift, sig.target + shift
        atr = float(ctx["df"]["atr"].iloc[-1])
        open_notional = sum(p["notional"] for p in self.broker.positions.values())
        sz = self.risk.size(self.equity, price, sig.stop, conf, atr / price, open_notional, slots)
        if not sz or sz["notional"] < 6:
            return
        market = "futures" if (sig.side == "short" or sz["leverage"] > 1 or conf >= 0.72) else "spot"
        pos = {"symbol": sym, "market": market, "side": sig.side, "qty": sz["qty"], "leverage": sz["leverage"],
               "stop": sig.stop, "init_stop": sig.stop, "target": sig.target, "atr": atr,
               "strategy": sig.strategy, "regime": ctx["regime"]["regime"], "features": feats,
               "reasons": sig.reasons + [why], "risk_usd": sz["risk_usd"], "conf": conf}
        try:
            pos = await self.broker.open(pos, price)
        except Exception as e:
            memory.log_event(f"❌ فشل فتح {sym}: {e}", "warn")
            return
        side_ar = "شراء" if sig.side == "long" else "بيع"
        memory.log_event(
            f"📈 فتح {side_ar} {sym} ({market} x{pos['leverage']}) | {STRATEGY_AR[sig.strategy]} | "
            f"ثقة {conf:.0%} | مخاطرة {sz['risk_pct']:.1%} | الأسباب: {'، '.join(sig.reasons[:4])}", "trade")

    # ================================================================ إدارة الصفقات
    async def manage_positions(self):
        for pos in list(self.broker.positions.values()):
            p = self.prices.get(pos["symbol"])
            if not p:
                continue
            long = pos["side"] == "long"
            hit_stop = p <= pos["stop"] if long else p >= pos["stop"]
            hit_tgt = p >= pos["target"] if long else p <= pos["target"]
            paper = self.mode == "paper"  # في المحاكاة: التنفيذ عند مستوى الأمر كما يحدث على المنصة
            if hit_stop:
                moved = abs(pos["stop"] - pos["init_stop"]) > 1e-12
                await self._close(pos, pos["stop"] if paper else p, "trailing" if moved else "stop")
                continue
            if hit_tgt:
                await self._close(pos, pos["target"] if paper else p, "target")
                continue
            risk = abs(pos["entry"] - pos["init_stop"])
            atr = pos["atr"]
            pos["best"] = max(pos["best"], p) if long else min(pos["best"], p)
            gain = (pos["best"] - pos["entry"]) if long else (pos["entry"] - pos["best"])
            if gain >= risk:  # وقف متحرك بعد +1R
                trail = 1.0 * atr if gain >= 2 * risk else 1.5 * atr
                ns = max(pos["entry"], pos["best"] - trail) if long else min(pos["entry"], pos["best"] + trail)
                better = ns > pos["stop"] + 0.1 * atr if long else ns < pos["stop"] - 0.1 * atr
                if better:
                    await self.broker.update_stop(pos, ns)
            # خروج زمني ذكي: 3 أيام بدون تقدم
            r_now = ((p - pos["entry"]) if long else (pos["entry"] - p)) / risk if risk else 0
            if time.time() - pos["opened"] > 72 * 3600 and r_now < 0.5:
                await self._close(pos, p, "timeout")
                continue
            reg = self.analysis.get(pos["symbol"], {}).get("regime")
            if (long and reg == TREND_DOWN or not long and reg == TREND_UP or reg == HIGH_RISK) and r_now > 0.3:
                await self._close(pos, p, "regime")

    async def _close(self, pos, price, reason):
        try:
            fill, pnl = await self.broker.close(pos, price, reason)
        except Exception as e:
            memory.log_event(f"❌ فشل إغلاق {pos['symbol']}: {e}", "error")
            return
        self._on_closed(pos, fill, pnl, reason)

    def _on_closed(self, pos, fill, pnl, reason):
        r = pnl / pos["risk_usd"] if pos.get("risk_usd") else 0
        memory.save_trade(pos | {"exit": fill, "pnl": pnl, "r_mult": r, "exit_reason": reason,
                                 "closed": time.time(), "mode": self.mode, "shadow": 0})
        self.learner.update(pos["strategy"], pos["regime"], r, weight=1.0)
        self.risk.on_close(r)
        icon = "✅" if pnl > 0 else "🔻"
        memory.log_event(f"{icon} إغلاق {pos['symbol']} عبر {EXIT_AR.get(reason, reason)}: "
                         f"{pnl:+.2f}$ ({r:+.2f}R)", "trade")
        if r <= 0:
            memory.log_event(f"🧠 درس محفوظ: {STRATEGY_AR[pos['strategy']]} في {LABELS_AR.get(pos['regime'])} "
                             f"خسرت — سيُرفض النمط المشابه إذا تكرر", "learn")

    async def kill(self, reason, manual=True):
        for pos in list(self.broker.positions.values()):
            await self._close(pos, self.prices.get(pos["symbol"], pos["entry"]), "kill")
        self.risk.freeze(reason, manual=manual)
        memory.log_event(f"🚨 مفتاح الطوارئ: {reason} — أُغلقت كل الصفقات وجُمِّد التداول", "error")

    # ================================================================ التجريب الموازي (ظل)
    def _open_shadow(self, sym, ctx, sig, feats):
        key = f"{sym}|{sig.strategy}"
        if any(v["key"] == key for v in self.shadow.values()):
            return
        sid = f"{key}|{int(time.time())}"
        self.shadow[sid] = {"key": key, "symbol": sym, "strategy": sig.strategy, "side": sig.side,
                            "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                            "atr": float(ctx["df"]["atr"].iloc[-1]), "ts": int(ctx["df"]["ts"].iloc[-1]),
                            "regime": ctx["regime"]["regime"], "features": feats, "reasons": sig.reasons}

    def _resolve_shadows(self, sym, df):
        for sid, s in list(self.shadow.items()):
            if s["symbol"] != sym:
                continue
            fut = df[df["ts"] > s["ts"]]
            if fut.empty:
                continue
            r, why, px, bars = simulate_trade(SimpleNamespace(**s), fut, s["atr"])
            if why == "timeout" and bars < 48:
                continue
            self.learner.update(s["strategy"], s["regime"], r, weight=0.7)
            memory.save_trade({**s, "market": "shadow", "qty": 0, "leverage": 1, "exit": px, "pnl": 0,
                               "r_mult": r, "exit_reason": why, "opened": s["ts"] / 1000,
                               "closed": time.time(), "mode": self.mode, "shadow": 1})
            self.shadow.pop(sid, None)

    # ================================================================ دراسة التاريخ
    async def pretrain_all(self):
        self.pretrain_status = "running"
        todo = [s for s in self.universe if s not in self.pretrained]
        for i, sym in enumerate(todo):
            self.status = f"يدرس تاريخ {sym} ({i + 1}/{len(todo)})"
            try:
                h1 = await ohlcv_df(self.spot, sym, "1h", settings.pretrain_bars)
                h4 = await ohlcv_df(self.spot, sym, "4h", 500)
                n = await asyncio.to_thread(pretrain, self.learner, sym, h1, h4)
                self.pretrained.add(sym)
                memory.kv_set("pretrained", sorted(self.pretrained))
                memory.log_event(f"📚 درس تاريخ {sym}: {n} صفقة افتراضية سابقة", "learn")
            except Exception as e:
                memory.log_event(f"⚠️ تعذر دراسة {sym}: {e}", "warn")
            await asyncio.sleep(1)
        self.pretrain_status = "done"
        memory.log_event("🎓 انتهت دراسة التاريخ — الوكيل جاهز للتداول", "learn")

    # ================================================================ للواجهة
    def state(self):
        prices = self.prices
        positions = []
        for p in self.broker.positions.values() if self.broker else []:
            px = prices.get(p["symbol"], p["entry"])
            d = 1 if p["side"] == "long" else -1
            upnl = (px - p["entry"]) * p["qty"] * d
            positions.append({k: p[k] for k in ("id", "symbol", "market", "side", "qty", "entry", "stop", "target",
                                                "leverage", "strategy", "opened", "reasons", "risk_usd")}
                             | {"price": px, "upnl": upnl, "r": upnl / p["risk_usd"] if p.get("risk_usd") else 0,
                                "strategy_ar": STRATEGY_AR[p["strategy"]]})
        return {
            "mode": self.mode, "running": self.running, "status": self.status,
            "frozen": self.risk.frozen, "frozen_reason": self.risk.frozen_reason,
            "equity": self.equity, "day_start": self.risk.day_start_equity,
            "daily_dd": self.risk.daily_dd(self.equity), "max_dd": settings.max_daily_drawdown,
            "loss_streak": self.risk.loss_streak, "risk_now": self.risk.risk_pct(0.7),
            "max_leverage": settings.max_leverage, "market_risk_off": self.market_risk_off,
            "liq_stream": self.liq.connected, "pretrain": self.pretrain_status,
            "pretrained": len(self.pretrained), "universe": len(self.universe),
            "positions": positions, "heartbeat": self.heartbeat,
            "analysis": sorted([a for s, a in self.analysis.items() if s in self.universe],
                               key=lambda a: (a.get("rank") or 999, a["symbol"])),
            "leaderboard": [r | {"strategy_ar": STRATEGY_AR.get(r["strategy"], r["strategy"]),
                                 "regime_ar": LABELS_AR.get(r["regime"], r["regime"])}
                            for r in self.learner.leaderboard()],
            "stats": memory.stats(0), "shadow_stats": memory.stats(1),
            "shadow_open": len(self.shadow),
            "memory": memory_usage(),
        }

    async def candles(self, sym, tf="15m"):
        df = await ohlcv_df(self.spot, sym, tf, 300)
        e = enrich(df).dropna().reset_index(drop=True)
        z = smc_mod.analyze(e)
        return {"candles": df.iloc[-200:].to_dict("records"),
                "fvgs": z["fvgs"], "order_blocks": z["order_blocks"],
                "trades": [p for p in self.state()["positions"] if p["symbol"] == sym]}
