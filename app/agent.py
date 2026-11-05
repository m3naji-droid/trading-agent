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
from .exchange import make_clients, build_universe, ohlcv_df, check_key_permissions, fut_symbol
from . import notify, performance
from .indicators import enrich
from .regime import detect_regime, HIGH_RISK, TREND_UP, TREND_DOWN, LABELS_AR
from . import smc as smc_mod
from .strategies import run_all, apply_orderflow, features, metrics, STRATEGY_AR, STRATEGIES, ORIGINAL
from .cross import add_btc_link, add_rs, add_funding, rs_panel
from .darwin import accepts
from . import study
from .mood import Mood
from .council import deliberate
from .darwin import Darwin, GENE_AR
from .ghosts import Ghosts
from .radar import WhaleRadar
from .orderflow import LiquidationTracker, snapshot
from .learner import Learner, simulate_trade
from .risk import RiskManager
from .broker import PaperBroker, LiveBroker
from datetime import datetime, timezone

REPORT_HOUR_UTC = 5  # 8 صباحاً بتوقيت البحرين

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
        self.mood = Mood()
        self.darwin = Darwin(list(STRATEGIES))
        self.ghosts = Ghosts()
        self.radar = WhaleRadar()
        self.liq.on_event = self.radar.add_liquidation
        self._blackout_key = None
        self._btc15 = None
        self._h4 = {}            # sym -> (وقت الجلب, شموع 4 ساعات)
        self._closes1h = {}      # base -> سلسلة الإغلاق (للقوة النسبية)
        self.rs_rank = {}
        self.funding, self._fund_t = {}, 0
        self.oi, self._oi_t = {}, {}
        self.gate = memory.kv_get("gate", {}) or {}
        self.study = memory.kv_get("study", {}) or {}
        self.study_progress = {"phase": "", "done": len(self.study.get("done", [])), "total": 0, "pct": 0.0}
        self._last_gate_check = 0
        self._studying = False
        self.universe, self.analysis, self.prices = [], {}, {}
        self.rank_info = {}
        self.restart_requested = False
        self._skip_log = {}
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
        asyncio.create_task(self.radar.run())
        asyncio.create_task(self.mood_loop())
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
                if backoff >= 80:  # أخطاء متكررة لعدة دقائق
                    notify.send("⚠️ الوكيل يواجه مشكلة", f"أخطاء متكررة منذ دقائق: {str(e)[:120]}\nالوكيل يعيد المحاولة تلقائياً.",
                                priority=4, tags=["warning"], throttle_key="cycle_error", throttle_sec=3 * 3600)
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
            self.radar.set_symbols(uni)
            if not self._studying:
                asyncio.create_task(self.study_all())

        watch = sorted(set(self.universe) | {p["symbol"] for p in self.broker.positions.values()})
        tick = await self.spot.fetch_tickers(watch)
        self.prices = {s: float(t["last"]) for s, t in tick.items() if t.get("last")}

        for pos, fill, pnl in await self.broker.sync(self.prices):
            self._on_closed(pos, fill, pnl, "exchange_stop")

        self.equity = await self.broker.equity(self.prices)
        if time.time() - self._last_equity_rec > 300:
            memory.record_equity(self.equity, self.prices.get("BTC/USDT"), self.mode)
            self._last_equity_rec = time.time()
        self.maybe_daily_report()
        self.maybe_dream()
        reason = self.risk.on_equity(self.equity)
        if reason:
            await self.kill(reason, manual=False)

        await self.refresh_derivatives()
        sem = asyncio.Semaphore(3)  # تحليل 3 عملات بالتوازي فقط لتوفير الذاكرة

        async def run(sym):
            async with sem:
                try:
                    return await self.analyze_symbol(sym)
                except Exception as e:
                    log.debug(f"{sym}: {e}")
                    return []

        first = await run("BTC/USDT") if "BTC/USDT" in self.universe else []  # البيتكوين أولاً (ظل البيتكوين)
        results = [first] + await asyncio.gather(*[run(s) for s in self.universe if s != "BTC/USDT"])
        self.update_rs()
        btc = self.analysis.get("BTC/USDT", {})
        risk_off = btc.get("regime") == HIGH_RISK
        if risk_off != self.market_risk_off:
            self.market_risk_off = risk_off
            memory.log_event("🛑 BTC في حالة خطورة عالية — إيقاف الدخول وحماية المحفظة" if risk_off
                             else "✅ زال خطر السوق العام — استئناف التحليل", "warn" if risk_off else "info")
            if risk_off:
                notify.send("🛑 السوق في حالة خطر", "البيتكوين في تذبذب شديد. الوكيل أوقف الدخول في صفقات جديدة لحماية المحفظة.",
                            priority=4, tags=["rotating_light"], throttle_key="risk_off", throttle_sec=6 * 3600)

        bo = self.mood.blackout()
        bkey = f"{bo['title']}|{bo['ts']}" if bo else None
        if bkey != self._blackout_key:
            self._blackout_key = bkey
            if bo:
                when = datetime.fromtimestamp(bo["ts"], timezone.utc).strftime("%H:%M UTC")
                memory.log_event(f"📅 حدث اقتصادي مهم ({bo['title']} — {when}): إيقاف الدخول مؤقتاً حتى ساعة بعده", "warn")
                notify.send("📅 إيقاف مؤقت قبل حدث اقتصادي", f"{bo['title']} الساعة {when}\nلا صفقات جديدة من نصف ساعة قبله حتى ساعة بعده.",
                            tags=["calendar"])
            else:
                memory.log_event("📅 انتهت فترة الحدث الاقتصادي — استئناف الدخول")

        await self.manage_positions()
        await self.decide([c for r in results for c in r])
        self.ghosts.tick(self.prices, self.prices.get("BTC/USDT"))
        for ev in self.darwin.pop_events():
            ch = "، ".join(f"{GENE_AR[k]} {ev['old'][k]}→{ev['new'][k]}" for k in ev["new"] if ev["new"][k] != ev["old"][k])
            memory.log_event(f"🧬 داروين: ترقية جيل {ev['generation']} لـ«{STRATEGY_AR[ev['strategy']]}» "
                             f"({ev['old_r']:+.2f}R → {ev['new_r']:+.2f}R): {ch}", "learn")
            notify.send(f"🧬 تطوّر استراتيجية: {STRATEGY_AR[ev['strategy']]}",
                        f"الجيل {ev['generation']}: متوسط {ev['old_r']:+.2f}R ← {ev['new_r']:+.2f}R\n{ch}", tags=["dna"])
        self.darwin.save()
        memory.kv_set("shadow", self.shadow)

    # ================================================================ التحليل
    async def analyze_symbol(self, sym):
        df15 = await ohlcv_df(self.spot, sym, settings.entry_tf, 300)
        h1 = await ohlcv_df(self.spot, sym, settings.trend_tf, 300)
        if len(df15) < 260 or len(h1) < 220:
            return []
        df15, h1 = df15.iloc[:-1], h1.iloc[:-1]  # الشموع المغلقة فقط

        base = sym.split("/")[0]
        h4 = await self.get_h4(sym)
        btc15 = self._btc15
        liq = self.liq.recent(sym)

        def compute():
            e15 = enrich(df15).dropna().reset_index(drop=True)
            e1 = enrich(h1).dropna().reset_index(drop=True)
            if base == "BTC":
                self._btc15 = e15
            elif btc15 is not None:
                e15 = add_btc_link(e15, btc15)
            e15 = add_rs(e15, self.rs_rank.get(base))
            e15 = add_funding(e15, self.funding.get(sym), self.oi.get(sym)).reset_index(drop=True)
            self._closes1h[base] = e1.set_index("ts")["close"].iloc[-200:]
            reg = detect_regime(e1)
            ctx = {"df": e15, "htf": e1, "ttf": h4, "regime": reg, "smc": smc_mod.analyze(e15), "flow": None,
                   "base": base, "liq": liq}
            strict = run_all(ctx)
            got = {x.strategy for x in strict}
            near = [x for x in run_all({**ctx, "relax": True}) if x.strategy not in got]
            return ctx, strict, near

        ctx, sigs, near = await asyncio.to_thread(compute)
        for sg in near:  # فرص قريبة: للتعلم فقط
            self._open_shadow(sym, ctx, sg, features(ctx, sg), None, near=True)
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
            "rs": self.rs_rank.get(base), "near": [x.strategy for x in near],
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
            allowed, why, pstats = memory.check_pattern(sig.strategy, sig.side, feats)
            cn = deliberate(sig, ctx, conf, self.risk, pstats, self.mood, sym.split("/")[0])
            self._open_shadow(sym, ctx, sig, feats, cn)
            self.analysis[sym]["last_decision"] = {"strategy": STRATEGY_AR[sig.strategy], "side": sig.side,
                                                   "conf": round(conf, 2), "memory": why, "allowed": allowed,
                                                   "council": cn["yes"], "votes": [v["icon"] + ("✓" if v["vote"] else "✗") for v in cn["votes"]]}
            if sym in self.pretrained and allowed and accepts(self.darwin.champion(sig.strategy), metrics(ctx, sig)):
                self.ghosts.consider(sym, sig, conf, cn["yes"], price, float(ctx["df"]["atr"].iloc[-1]),
                                     self.darwin.champion(sig.strategy), self.market_risk_off,
                                     self.mood.blackout(), self.prices)
            if not allowed:
                memory.log_event(f"🧠 رفض {sym} {STRATEGY_AR[sig.strategy]}: {why}")
                continue
            gst = self.gate_status(sig.strategy)
            champ = self.darwin.champion(sig.strategy)
            if conf >= settings.min_confidence and sym not in open_syms and gst != "approved":
                self._throttled(f"gate_{sig.strategy}", 6 * 3600,
                                f"🛡️ {STRATEGY_AR[sig.strategy]}: فرصة في {sym} لكن الاستراتيجية لم تُعتمد بعد "
                                f"({self.gate.get(sig.strategy, {}).get('reason', 'تحت الاختبار')}) — تُسجَّل للتعلم فقط")
                continue
            if conf >= settings.min_confidence and sym not in open_syms and not accepts(champ, metrics(ctx, sig)):
                self._throttled(f"dw_{sym}_{sig.strategy}", 3600,
                                f"🧬 فلتر داروين تجاوز {sym} ({STRATEGY_AR[sig.strategy]}): الإشارة لا تحقق شروط الدخول المتطورة")
                continue
            if conf >= settings.min_confidence and sym not in open_syms:
                if not cn["approved"]:
                    no = [v for v in cn["votes"] if not v["vote"]]
                    key = f"council_{sym}_{sig.strategy}"
                    if time.time() - self._skip_log.get(key, 0) > 3600:
                        self._skip_log[key] = time.time()
                        memory.log_event(f"🏛️ المجلس رفض {sym} ({STRATEGY_AR[sig.strategy]}) {cn['yes']}/3 — "
                                         + " | ".join(f"{v['icon']} {v['name']}: {v['reason']}" for v in no))
                    continue
                scored.append((conf * min(sig.rr, 3), conf, sym, ctx, sig, feats, why, cn))

        if time.time() - self._last_gate_check > 3600:
            self._last_gate_check = time.time()
            for st_ in study.live_redemption(self.gate, self.mode):
                memory.log_event(f"🛡️ اعتماد «{STRATEGY_AR[st_]}»: أثبتت نفسها في التداول التجريبي الحي", "learn")
                notify.send("🛡️ استراتيجية جديدة معتمدة", f"«{STRATEGY_AR[st_]}» أثبتت نفسها حياً وصار الوكيل يتداول بها.",
                            tags=["shield"])
            memory.kv_set("gate", self.gate)
        if (not self.running or self.risk.frozen or self.market_risk_off or self.pretrain_status == "pending"
                or self.mood.blackout()):
            return
        scored.sort(key=lambda x: -x[0])
        for _, conf, sym, ctx, sig, feats, why, cn in scored:
            slots = settings.max_open_positions - len(self.broker.positions)
            if slots <= 0:
                break
            if sym in {p["symbol"] for p in self.broker.positions.values()}:
                continue
            if sym not in self.pretrained:
                continue  # لا يتداول عملة قبل دراسة تاريخها
            await self.open_position(sym, ctx, sig, conf, feats, why, slots, cn)

    async def open_position(self, sym, ctx, sig, conf, feats, why, slots, cn=None):
        price = self.prices.get(sym, sig.entry)
        # إعادة حساب الوقف والهدف على السعر الحالي بنفس المسافات، ثم تطبيق جينات داروين للبطل
        genome = self.darwin.champion(sig.strategy)
        risk0 = abs(sig.entry - sig.stop)
        rr0 = abs(sig.target - sig.entry) / risk0 if risk0 else 0
        d = 1 if sig.side == "long" else -1
        dist = risk0 * genome["stop"]
        sig.entry, sig.stop, sig.target = price, price - d * dist, price + d * dist * rr0 * genome["rr"]
        atr = float(ctx["df"]["atr"].iloc[-1])
        open_notional = sum(p["notional"] for p in self.broker.positions.values())
        sz = self.risk.size(self.equity, price, sig.stop, conf, atr / price, open_notional, slots)
        if not sz or sz["notional"] < 6:
            return
        market = "futures" if (sig.side == "short" or sz["leverage"] > 1 or conf >= 0.72) else "spot"
        market, sz = self._fit_min_notional(sym, market, sig, sz, price)
        if not sz:
            return
        pos = {"symbol": sym, "market": market, "side": sig.side, "qty": sz["qty"], "leverage": sz["leverage"],
               "stop": sig.stop, "init_stop": sig.stop, "target": sig.target, "atr": atr,
               "strategy": sig.strategy, "regime": ctx["regime"]["regime"], "features": feats,
               "reasons": sig.reasons + [why], "risk_usd": sz["risk_usd"], "conf": conf,
               "genome": genome, "council": cn}
        try:
            pos = await self.broker.open(pos, price)
        except Exception as e:
            memory.log_event(f"❌ فشل فتح {sym}: {e}", "warn")
            return
        side_ar = "شراء" if sig.side == "long" else "بيع"
        memory.log_event(
            f"📈 فتح {side_ar} {sym} ({market} x{pos['leverage']}) | {STRATEGY_AR[sig.strategy]} | "
            f"ثقة {conf:.0%} | مخاطرة {sz['risk_pct']:.1%} | الأسباب: {'، '.join(sig.reasons[:4])}", "trade")
        notify.send(f"📈 {side_ar} {sym.split('/')[0]} ({market} x{pos['leverage']})",
                    f"{STRATEGY_AR[sig.strategy]} · ثقة {conf:.0%}\nدخول {pos['entry']:.6g} · وقف {pos['stop']:.6g} · هدف {pos['target']:.6g}\n"
                    f"المخاطرة: {pos['risk_usd']:.2f}$\n" + "، ".join(sig.reasons[:3]), tags=["chart_with_upwards_trend"])

    # ================================================================ الحد الأدنى للصفقة في Binance
    def _min_cost(self, sym, market):
        ex, s = (self.fut, fut_symbol(sym)) if market == "futures" else (self.spot, sym)
        m = (getattr(ex, "markets", None) or {}).get(s) or {}
        lim = ((m.get("limits") or {}).get("cost") or {}).get("min")
        if not lim:
            for f in (m.get("info") or {}).get("filters", []) or []:
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    lim = float(f.get("notional") or f.get("minNotional") or 0)
        return float(lim or 5) * 1.05  # هامش أمان 5%

    def _fit_min_notional(self, sym, market, sig, sz, price):
        """
        رأس المال الصغير: بعض العقود تشترط قيمة دنيا كبيرة للصفقة.
        1) يجرب السوق الآخر (Spot) إن كان ممكناً. 2) يرفع الحجم للحد الأدنى فقط إذا بقيت المخاطرة ضمن الحد الأقصى.
        3) وإلا يتجاوز الفرصة ويسجّل السبب.
        """
        need = self._min_cost(sym, market)
        if sz["notional"] >= need:
            return market, sz
        if market == "futures" and sig.side == "long" and sz["leverage"] == 1:
            need_spot = self._min_cost(sym, "spot")
            if sz["notional"] >= need_spot:
                return "spot", sz
        scale = need / sz["notional"]
        new_risk = sz["risk_usd"] * scale
        free = self.equity - sum(p["notional"] / p["leverage"] for p in self.broker.positions.values())
        if new_risk <= self.equity * settings.risk_max and need / sz["leverage"] <= free * 0.95:
            sz = dict(sz, qty=sz["qty"] * scale, notional=need, risk_usd=new_risk, margin=need / sz["leverage"],
                      risk_pct=new_risk / self.equity)
            return market, sz
        key = f"minskip_{sym}_{market}"
        if time.time() - self._skip_log.get(key, 0) > 6 * 3600:
            self._skip_log[key] = time.time()
            memory.log_event(f"💡 تجاوز {sym} ({market}): الحد الأدنى للصفقة في Binance {need:.0f}$ "
                             f"يتطلب مخاطرة {new_risk / max(self.equity, 1e-9):.1%} — أعلى من الحد المسموح {settings.risk_max:.0%}")
        return market, None

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
            g = pos.get("genome") or {"trail": 1.5, "be": 1.0}
            if gain >= g["be"] * risk:  # وقف متحرك وفق جينات داروين
                trail = g["trail"] * atr * (0.67 if gain >= 2 * risk else 1.0)
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
        memory.save_trade(pos | {"stop": pos.get("init_stop", pos["stop"]), "exit": fill, "pnl": pnl, "r_mult": r, "exit_reason": reason,
                                 "closed": time.time(), "mode": self.mode, "shadow": 0})
        self.learner.update(pos["strategy"], pos["regime"], r, weight=1.0)
        self.risk.on_close(r)
        icon = "✅" if pnl > 0 else "🔻"
        memory.log_event(f"{icon} إغلاق {pos['symbol']} عبر {EXIT_AR.get(reason, reason)}: "
                         f"{pnl:+.2f}$ ({r:+.2f}R)", "trade")
        notify.send(f"{icon} إغلاق {pos['symbol'].split('/')[0]}: {pnl:+.2f}$ ({r:+.2f}R)",
                    f"{STRATEGY_AR[pos['strategy']]} · عبر {EXIT_AR.get(reason, reason)}\nالمحفظة: ${self.equity:,.2f}",
                    tags=["white_check_mark" if pnl > 0 else "small_red_triangle_down"])
        if r <= 0:
            memory.log_event(f"🧠 درس محفوظ: {STRATEGY_AR[pos['strategy']]} في {LABELS_AR.get(pos['regime'])} "
                             f"خسرت — سيُرفض النمط المشابه إذا تكرر", "learn")

    async def kill(self, reason, manual=True):
        for pos in list(self.broker.positions.values()):
            await self._close(pos, self.prices.get(pos["symbol"], pos["entry"]), "kill")
        self.risk.freeze(reason, manual=manual)
        memory.log_event(f"🚨 مفتاح الطوارئ: {reason} — أُغلقت كل الصفقات وجُمِّد التداول", "error")
        notify.send("🚨 مفتاح الطوارئ", f"{reason}\nأُغلقت كل الصفقات وجُمِّد التداول.", priority=5, tags=["rotating_light"])

    # ================================================================ التقرير اليومي
    def maybe_daily_report(self):
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour < REPORT_HOUR_UTC or memory.kv_get("last_report") == today:
            return
        memory.kv_set("last_report", today)
        try:
            extra = f"🌍 حالة BTC: {self.analysis.get('BTC/USDT', {}).get('label', '—')}"
            title, text = performance.daily_report(self.mode, self.equity, extra)
        except Exception as e:
            log.warning(f"daily report: {e}")
            return
        reps = memory.kv_get("reports", [])
        reps.insert(0, {"ts": time.time(), "title": title, "text": text})
        memory.kv_set("reports", reps[:14])
        memory.log_event(f"📰 {title}")
        notify.send(f"📰 {title}", text, tags=["newspaper"])

    # ================================================================ التجريب الموازي (ظل)
    def _open_shadow(self, sym, ctx, sig, feats, council=None, near=False):
        key = f"{sym}|{sig.strategy}"
        if any(v["key"] == key for v in self.shadow.values()):
            return
        sid = f"{key}|{int(time.time())}"
        self.shadow[sid] = {"key": key, "symbol": sym, "strategy": sig.strategy, "side": sig.side,
                            "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                            "atr": float(ctx["df"]["atr"].iloc[-1]), "ts": int(ctx["df"]["ts"].iloc[-1]),
                            "regime": ctx["regime"]["regime"], "features": feats, "reasons": sig.reasons,
                            "council": council, "m": metrics(ctx, sig), "near": near}

    def _resolve_shadows(self, sym, df):
        for sid, s in list(self.shadow.items()):
            if s["symbol"] != sym:
                continue
            fut = df[df["ts"] > s["ts"]]
            if fut.empty:
                continue
            sg = SimpleNamespace(**s)
            champ = self.darwin.champion(s["strategy"])
            r, why, px, bars = simulate_trade(sg, fut, s["atr"], genome=champ)
            if why == "timeout" and bars < 48:
                continue
            w = 0.3 if s.get("near") else 1.0
            self.darwin.record(s["strategy"], self.darwin.evaluate(s["strategy"], sg, s.get("m"), fut, s["atr"],
                                                                   simulate_trade), weight=w)
            self.learner.update(s["strategy"], s["regime"], r, weight=0.3 if s.get("near") else 0.7)
            memory.save_trade({**s, "market": "near" if s.get("near") else "shadow", "qty": 0, "leverage": 1, "exit": px, "pnl": 0,
                               "r_mult": r, "exit_reason": why, "opened": s["ts"] / 1000,
                               "closed": time.time(), "mode": self.mode, "shadow": 1})
            self.shadow.pop(sid, None)

    # ================================================================ دراسة التاريخ
    # ================================================================ دراسة سنتين + الاختبار المخفي
    async def study_all(self):
        self._studying = True
        try:
            await self._study_all()
        except Exception as e:
            log.exception("study_all")
            memory.log_event(f"⚠️ توقفت الدراسة مؤقتاً: {str(e)[:120]} — ستُستأنف لاحقاً", "warn")
            if self.pretrained:
                self.pretrain_status = "done"
        finally:
            self._studying = False

    async def _study_all(self):
        """
        يدرس سنتين من تاريخ كل عملة في الخلفية (يُستأنف تلقائياً بعد أي إعادة تشغيل).
        المرحلة 1: تحميل/تحديث الشموع على القرص. المرحلة 2: المحاكاة + بوابة الاعتماد.
        """
        self.pretrain_status = "running" if not self.pretrained else "done"
        st = self.study
        if st.get("version") != 2:
            # بداية جديدة: مسح الدراسة القديمة القصيرة (6 أسابيع) وإعادة التعلم من سنتين
            with memory._lock, memory._conn() as c:
                c.execute("DELETE FROM trades WHERE mode='pretrain'")
            self.learner.reset()
            st = self.study = {"version": 2, "done": [], "agg": {}, "started": time.time()}
            memory.kv_set("study", st)
            memory.log_event("📚 بدء دراسة سنتين من تاريخ كل عملة + الاختبار على المستقبل المخفي (تعمل في الخلفية)", "learn")
        todo = [s_ for s_ in self.universe if s_.split("/")[0] not in st["done"]]
        if not todo:
            self.pretrain_status = "done"
            return
        bars = settings.pretrain_bars
        raw1, raw4, fund = {}, {}, {}
        self.study_progress.update(phase="تحميل التاريخ", total=len(self.universe))
        for i, sym in enumerate(self.universe):
            base = sym.split("/")[0]
            self.study_progress.update(pct=i / len(self.universe), current=base)
            try:
                raw1[base] = await study.update_history(self.spot, sym, "1h", bars)
                if sym in todo:
                    raw4[base] = await study.update_history(self.spot, sym, "4h", bars // 4)
                    fund[base] = await study.update_funding(self.fut, sym, bars // 24 + 2)
            except Exception as e:
                memory.log_event(f"⚠️ تعذر تحميل تاريخ {sym}: {e}", "warn")
            await asyncio.sleep(0.5)
        panel = await asyncio.to_thread(rs_panel, {b: d.set_index("ts")["close"] for b, d in raw1.items() if len(d)})
        btc_e = enrich(raw1["BTC"]).dropna().reset_index(drop=True) if "BTC" in raw1 and len(raw1["BTC"]) else None
        self.study_progress.update(phase="المحاكاة والاختبار المخفي")
        for i, sym in enumerate(todo):
            base = sym.split("/")[0]
            if base not in raw1 or base not in raw4 or len(raw1[base]) < 400:
                continue
            self.status = f"يدرس سنتين من تاريخ {base} ({i + 1}/{len(todo)})"
            ts = raw1[base]["ts"].values
            split = ts[int(len(ts) * (1 - study.TEST_SHARE))]
            prog = lambda p, i=i: self.study_progress.update(pct=(i + p) / len(todo), current=base)
            try:
                stats, n = await asyncio.to_thread(
                    study.simulate_symbol, base, raw1[base], raw4[base], btc_e,
                    panel[base] if base in panel else None, fund.get(base), split, self.darwin, self.learner,
                    step=2, recent_from=time.time() * 1000 - 30 * 86_400_000, progress=prog)
            except Exception as e:
                log.exception("study")
                memory.log_event(f"⚠️ تعذرت دراسة {base}: {e}", "warn")
                continue
            study.merge_stats(st["agg"], stats)
            st["done"].append(base)
            memory.kv_set("study", st)
            self.pretrained.add(sym)
            memory.kv_set("pretrained", sorted(self.pretrained))
            raw4.pop(base, None)
            memory.log_event(f"📚 درس سنتين من {base}: {n} إشارة تاريخية", "learn")
            gc.collect()
        if len(st["done"]) < max(1, min(3, len(self.universe) // 3)):
            memory.log_event("⚠️ لم يكتمل تحميل التاريخ لعدد كافٍ من العملات — ستُعاد المحاولة لاحقاً", "warn")
            self.pretrain_status = "done" if self.pretrained else "running"
            return
        self.gate = study.compute_gate(st["agg"], self.gate)
        study.apply_recent(self.gate, st["agg"])
        memory.kv_set("gate", self.gate)
        st["finished"] = time.time()
        memory.kv_set("study", st)
        memory.kv_set("darwin_seeded", True)
        self.pretrain_status = "done"
        self.study_progress.update(phase="مكتملة", pct=1.0, current="")
        ok = [STRATEGY_AR[k] for k, g in self.gate.items() if g["status"] == "approved"]
        memory.log_event(f"🎓 انتهت دراسة السنتين. معتمدة للتداول: {'، '.join(ok) or 'لا شيء بعد'}", "learn")
        notify.send("🎓 انتهت دراسة السنتين", f"اجتازت الاختبار المخفي: {'، '.join(ok) or 'لا شيء بعد'}\nالبقية تُختبر حياً.",
                    tags=["mortar_board"])

    def gate_status(self, strategy):
        g = self.gate.get(strategy)
        if g:
            return g["status"]
        return "approved" if strategy in ORIGINAL else "testing"  # قبل انتهاء الدراسة: القديمة فقط

    async def dream(self):
        """🌙 حلم الوكيل: إعادة محاكاة آخر 30 يوماً بكل الاستراتيجيات والجينات."""
        memory.log_event("🌙 الوكيل يحلم: إعادة محاكاة آخر 30 يوماً…", "learn")
        agg, total = {}, 0
        raw1 = {}
        for sym in self.universe:
            try:
                raw1[sym.split("/")[0]] = await study.update_history(self.spot, sym, "1h", settings.pretrain_bars)
            except Exception:
                pass
        panel = await asyncio.to_thread(rs_panel, {b: d.set_index("ts")["close"] for b, d in raw1.items() if len(d)})
        btc_e = enrich(raw1["BTC"].iloc[-1500:]).dropna().reset_index(drop=True) if "BTC" in raw1 else None
        since = time.time() * 1000 - 30 * 86_400_000
        for sym in self.universe:
            base = sym.split("/")[0]
            if base not in raw1 or len(raw1[base]) < 600:
                continue
            try:
                r4 = await study.update_history(self.spot, sym, "4h", settings.pretrain_bars // 4)
                f = await study.update_funding(self.fut, sym, settings.pretrain_bars // 24 + 2)
                r1 = raw1[base].iloc[-1500:]  # سياق كافٍ قبل آخر 30 يوماً
                stats, n = await asyncio.to_thread(
                    study.simulate_symbol, base, r1, r4.iloc[-600:], btc_e,
                    panel[base] if base in panel else None, f, float("inf"), self.darwin, self.learner,
                    start_ts=since, save=False, lw=0.1, dw=0.3, recent_from=since)
                study.merge_stats(agg, stats, keys=("recent",))
                total += n
            except Exception as e:
                log.warning(f"dream {sym}: {e}")
            await asyncio.sleep(1)
        changes = study.apply_recent(self.gate, agg)
        memory.kv_set("gate", self.gate)
        memory.kv_set("last_dream", {"ts": time.time(), "signals": total})
        for s_, stt in changes:
            msg = (f"⛔ تعليق مؤقت لـ«{STRATEGY_AR[s_]}»: تخسر في السوق الحالي" if stt == "suspended"
                   else f"✅ عودة «{STRATEGY_AR[s_]}»: تعافت في آخر 30 يوماً")
            memory.log_event(msg, "learn")
            notify.send("🌙 نتيجة حلم الوكيل", msg, tags=["crescent_moon"])
        memory.log_event(f"🌙 انتهى الحلم: {total} فرصة أُعيدت محاكاتها", "learn")

    def maybe_dream(self):
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == 1 and self.pretrain_status == "done" and self.study.get("finished") \
                and memory.kv_get("dream_day") != today:
            memory.kv_set("dream_day", today)
            asyncio.create_task(self.dream())

    # ================================================================ بيانات مشتركة
    async def get_h4(self, sym):
        t, df = self._h4.get(sym, (0, None))
        if time.time() - t > 900 or df is None:
            try:
                raw = await ohlcv_df(self.spot, sym, "4h", 260)
                df = await asyncio.to_thread(lambda: enrich(raw.iloc[:-1]).dropna().reset_index(drop=True))
                self._h4[sym] = (time.time(), df)
            except Exception:
                pass
        return df

    async def refresh_derivatives(self):
        if time.time() - self._fund_t > 300:
            try:
                rates = await self.fut.fetch_funding_rates()
                self.funding = {k.split(":")[0]: float(v.get("fundingRate") or 0) for k, v in rates.items()}
                self._fund_t = time.time()
            except Exception as e:
                log.debug(f"funding: {e}")
        # العقود المفتوحة: 5 عملات كل دورة، كل عملة كل 30 دقيقة
        due = [s_ for s_ in self.universe if time.time() - self._oi_t.get(s_, 0) > 1800][:5]
        for sym in due:
            self._oi_t[sym] = time.time()
            try:
                h = await self.fut.fetch_open_interest_history(fut_symbol(sym), "1h", limit=25)
                v = [float(x.get("openInterestValue") or x.get("openInterestAmount") or 0) for x in h]
                if len(v) >= 2 and v[0] > 0:
                    self.oi[sym] = v[-1] / v[0] - 1
            except Exception:
                pass

    def update_rs(self):
        if len(self._closes1h) < 3 or "BTC" not in self._closes1h:
            return
        try:
            p = rs_panel(dict(self._closes1h))
            last = p.iloc[-1].dropna()
            self.rs_rank = {k: float(v) for k, v in last.items()}
        except Exception as e:
            log.debug(f"rs: {e}")

    def _throttled(self, key, sec, msg, level="info"):
        if time.time() - self._skip_log.get(key, 0) > sec:
            self._skip_log[key] = time.time()
            memory.log_event(msg, level)

    # ================================================================ مزاج السوق
    async def mood_loop(self):
        while True:
            try:
                bases = [s.split("/")[0] for s in self.universe] or ["BTC", "ETH"]
                await asyncio.to_thread(self.mood.refresh, bases)
            except Exception as e:
                log.warning(f"mood: {e}")
            await asyncio.sleep(120)

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
            "mood": self.mood.summary({p["symbol"].split("/")[0] for p in self.broker.positions.values()}
                                      if self.broker else set()),
            "darwin": self.darwin.summary(),
            "ghosts": self.ghosts.summary(self.prices),
            "council_stats": memory.council_stats(self.mode),
            "radar_on": self.radar.connected,
            "gate": {k: {"status": self.gate_status(k), **{x: y for x, y in self.gate.get(k, {}).items() if x != "ts"}}
                     for k in STRATEGIES},
            "strategy_names": STRATEGY_AR,
            "study": {**self.study_progress, "status": self.pretrain_status,
                      "done": len(self.study.get("done", [])), "total": len(self.universe),
                      "finished": self.study.get("finished"), "years": round(settings.pretrain_bars / 8760, 1)},
            "dream": memory.kv_get("last_dream"),
        }

    async def candles(self, sym, tf="15m"):
        df = await ohlcv_df(self.spot, sym, tf, 300)
        e = enrich(df).dropna().reset_index(drop=True)
        z = smc_mod.analyze(e)
        return {"candles": df.iloc[-200:].to_dict("records"),
                "fvgs": z["fvgs"], "order_blocks": z["order_blocks"],
                "trades": [p for p in self.state()["positions"] if p["symbol"] == sym]}
