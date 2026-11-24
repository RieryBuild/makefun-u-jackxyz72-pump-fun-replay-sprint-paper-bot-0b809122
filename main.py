"""Pump.fun Replay Sprint — paper trading bot (pure Python stdlib).

Strategy, in brief: buy quality launches early, take the profit, never
chase. A coin qualifies only while it is still young (first 30 trades)
and only when its early flow is buy-heavy (cumulative buys >= 3 SOL and
at least twice cumulative sells), dispersed (7+ distinct buyers, no
single buyer over 35% of the buys), and honest (the creator put in
0.05–1 SOL of their own money and has not sold). The position then
exits whole on take-profit (+25%), stop-loss (-15%), a tight trailing
stop that arms at +5% and sells on a 5% pullback from the peak, a
hold-time cap, staleness, or a creator sell. Up to four entries per coin
are allowed while it stays in its early window, so a stopped-out coin
that re-qualifies can be re-entered. A separate probe mode relaxes the
bars when round-tripped coins lag the pace of the window, so the
20-coin guardrail stays in reach.

Why these bars: offline screening over the dev window (latency, size
cap, slippage rejections and fees all modelled) showed that chasing
mature momentum loses money, that riding winners uncapped loses money,
and that the creator-skin + dispersed-ownership filters are what turn
the early-entry edge positive. The 15% slippage bound is kept on purpose:
widening it lets the bot fill into pumps that have already run away.

Protocol: the harness speaks agent-fun/replay/v2 over stdio — one hello,
then one batch per slot carrying events, marks and fills, then done. This
file owns the strategy only; vendor/makefun_bot.py owns the wire. Every
decision here is a pure function of the batches seen so far: no clock,
no randomness, no network, no environment reads, and every iteration
that affects order is over insertion-ordered dicts or an explicit sort,
so two graded runs produce byte-identical action logs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

SOL = 1_000_000_000

# --- sizing (lamports) -------------------------------------------------------
FIRST_BUY = 80_000_000          # 0.08 SOL entry; capped by the size cap
MIN_BUY = 10_000_000            # 0.01 SOL: below this a round trip is no good
MAX_SLIPPAGE_BPS = 1500

# --- quality entry bars ------------------------------------------------------
MAX_EARLY_TRADES = 30           # only the coin's first 30 trades qualify
MIN_EARLY_BUY_SOL = 3_000_000_000   # cumulative buys in the early window
MIN_BUY_SELL_NUM = 2              # cumulative buys vs cumulative sells, as
MIN_BUY_SELL_DEN = 1              # an integer ratio (2:1) — no floats anywhere
MIN_BUYERS = 7
MAX_TOP_BUYER_SHARE_BPS = 3500  # no single buyer over 35% of the buys
CREATOR_BUY_MIN = 50_000_000    # creator skin in the game: 0.05 SOL
CREATOR_BUY_MAX = 1_000_000_000  # ...but over 1 SOL smells bundled

# --- probe mode (relaxed bars, used only when round trips lag the pace) ------
PROBE_MAX_EARLY_TRADES = 60
PROBE_MIN_EARLY_BUY_SOL = 1_000_000_000
PROBE_MIN_BUY_SELL_NUM = 3
PROBE_MIN_BUY_SELL_DEN = 2
PROBE_MIN_BUYERS = 5
PROBE_MAX_TOP_BUYER_SHARE_BPS = 5000
PROBE_CREATOR_BUY_MIN = 20_000_000
PROBE_CREATOR_BUY_MAX = 2_000_000_000

# --- exits (all comparisons in integer basis points; no floats anywhere,
# so two graded runs cannot diverge on rounding) ---------------------------
TAKE_PROFIT_BPS = 12500         # +25%
STOP_LOSS_BPS = 8500            # -15%
TRAIL_ARM_BPS = 10500           # trailing stop arms once peak reaches this
TRAIL_FACTOR_BPS = 9500         # sell when value falls to 95% of peak
MAX_HOLD_SLOTS = 7200           # ~48 min
STALE_SLOTS = 900               # no trade for this long -> exit

MAX_TRADES_KEPT = 80
MAX_BUYERS_KEPT = 150
PRUNE_AFTER_SLOTS = 4000        # drop idle unheld coins this stale
MAX_REENTRIES = 4


class Coin:
    """Per-mint flow state, built only from the batches the harness sends."""

    __slots__ = (
        "mint", "created_slot", "creator", "complete", "last_trade_slot",
        "trade_count", "trades", "buyers", "cum_buy_sol",
        "cum_sell_sol", "creator_buy_sol", "creator_sold", "entries",
    )

    def __init__(self, mint, created_slot, creator):
        self.mint = mint
        self.created_slot = created_slot
        self.creator = creator
        self.complete = False
        self.last_trade_slot = created_slot if created_slot is not None else -1
        self.trade_count = 0
        self.trades = []        # (slot, is_buy, sol_lamports), oldest first
        self.buyers = {}        # user -> buy lamports
        self.cum_buy_sol = 0
        self.cum_sell_sol = 0
        self.creator_buy_sol = 0
        self.creator_sold = False
        self.entries = 0

    def note_trade(self, slot, is_buy, sol, user, vsol, vtok):
        self.last_trade_slot = slot
        self.trade_count += 1
        self.trades.append((slot, is_buy, sol))
        if len(self.trades) > MAX_TRADES_KEPT:
            del self.trades[0]
        if is_buy:
            self.cum_buy_sol += sol
            if user == self.creator:
                self.creator_buy_sol += sol
            prev = self.buyers.get(user)
            if prev is None:
                if len(self.buyers) < MAX_BUYERS_KEPT:
                    self.buyers[user] = sol
            else:
                self.buyers[user] = prev + sol
        else:
            self.cum_sell_sol += sol
            if user == self.creator:
                self.creator_sold = True

    def top_buyer_share_bps(self):
        """Top buyer's share of all buys, in basis points (integer)."""
        if self.cum_buy_sol <= 0 or not self.buyers:
            return 10000
        top = 0
        for v in self.buyers.values():
            if v > top:
                top = v
        return top * 10000 // self.cum_buy_sol


class Bot:
    def __init__(self):
        self.coins = {}         # mint -> Coin, first-seen order
        self.pos = {}           # mint -> [entry_slot, peak_value_lamports]
        self.bought = set()     # mints with a filled buy
        self.sold = set()       # mints with a filled sell
        self.round_trip_count = 0
        self.batches = 0

    # -- round trips ----------------------------------------------------------
    def round_trips(self):
        return self.round_trip_count

    def probe_mode(self):
        """True when round-tripped coins lag the pace of the window."""
        rt = self.round_trips()
        if rt >= 20:
            return False
        # Batches are slots carrying records; the window is ~24h. Probe
        # tiers fire as the window advances with too few round trips.
        if self.batches > 20000 and rt < 15:
            return True
        if self.batches > 8000 and rt < 10:
            return True
        if self.batches > 3000 and rt < 6:
            return True
        return False

    # -- tracking -------------------------------------------------------------
    def _coin(self, mint, slot, creator=None):
        coin = self.coins.get(mint)
        if coin is None:
            coin = Coin(mint, slot, creator)
            self.coins[mint] = coin
        return coin

    def track(self, batch):
        for e in batch["events"]:
            kind = e["event"]
            mint = e["mint"]
            if kind == "CreateEvent":
                coin = self._coin(mint, e["slot"], e.get("creator"))
                if coin.created_slot is None:
                    coin.created_slot = e["slot"]
                if coin.creator is None:
                    coin.creator = e.get("creator")
            elif kind == "TradeEvent":
                coin = self._coin(mint, e["slot"], e.get("creator"))
                if coin.creator is None:
                    coin.creator = e.get("creator")
                coin.note_trade(
                    e["slot"], e["is_buy"], int(e["sol_amount"]),
                    e["user"], int(e["virtual_sol_reserves"]),
                    int(e["virtual_token_reserves"]),
                )
            else:  # CompleteEvent
                coin = self.coins.get(mint)
                if coin is not None:
                    coin.complete = True
        for f in batch["fills"]:
            if f["status"] != "filled":
                continue
            mint = f["mint"]
            if f["side"] == "buy":
                if mint not in self.bought:
                    self.bought.add(mint)
                    if mint in self.sold:
                        self.round_trip_count += 1
            elif f["side"] == "sell":
                if mint not in self.sold:
                    self.sold.add(mint)
                    if mint in self.bought:
                        self.round_trip_count += 1

    def prune(self, slot):
        stale = []
        for mint, coin in self.coins.items():
            if mint in self.pos:
                continue
            if slot - coin.last_trade_slot > PRUNE_AFTER_SLOTS:
                stale.append(mint)
        for mint in stale:
            del self.coins[mint]

    # -- valuation ------------------------------------------------------------
    @staticmethod
    def mark_value(state, mint, holding, curve):
        """Current mark value of a held position, in lamports (integer)."""
        if curve is None or curve.virtual_token_reserves <= 0:
            return 0
        return (holding.token_amount * curve.virtual_quote_reserves
                // curve.virtual_token_reserves)

    # -- decisions ------------------------------------------------------------
    def on_batch(self, state, batch):
        self.batches += 1
        self.track(batch)
        slot = state.slot
        if self.batches % 512 == 0:
            self.prune(slot)

        orders = []
        caps = state.hello["caps"]
        max_orders = caps["max_orders_per_batch"]
        tx = state.tx_fee_lamports()
        free = state.free_cash_lamports()
        slots_open = caps["max_open_positions"] - state.committed_position_count()

        # Sync position bookkeeping with the account the harness reports.
        for mint in list(self.pos):
            if mint not in state.positions:
                del self.pos[mint]
        for mint in state.positions:
            if mint not in self.pos:
                self.pos[mint] = [slot, 0]

        # 1) Exits first: whole-position sells, in mint order (deterministic).
        #    All comparisons are integer basis-point math on lamports.
        for mint in sorted(state.positions):
            holding = state.positions[mint]
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            coin = self.coins.get(mint)
            cost = holding.cost_lamports
            value = self.mark_value(state, mint, holding, curve)
            rec = self.pos[mint]
            if value > rec[1]:
                rec[1] = value
            peak = rec[1]
            do_exit = False
            if cost > 0 and value * 10000 >= cost * TAKE_PROFIT_BPS:
                do_exit = True
            elif 0 < value <= cost * STOP_LOSS_BPS // 10000:
                do_exit = True
            elif (cost > 0 and peak * 10000 >= cost * TRAIL_ARM_BPS
                    and value * 10000 <= peak * TRAIL_FACTOR_BPS):
                do_exit = True
            elif slot - rec[0] >= MAX_HOLD_SLOTS:
                do_exit = True
            elif coin is not None and slot - coin.last_trade_slot >= STALE_SLOTS:
                do_exit = True
            elif coin is not None and coin.creator_sold:
                do_exit = True
            if do_exit and len(orders) < max_orders:
                orders.append({
                    "side": "sell", "mint": mint,
                    "token_amount": holding.token_amount,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
                free -= tx
        exiting = {o["mint"] for o in orders if o["side"] == "sell"}

        # 2) Fresh entries: only coins that traded in this batch, in batch
        #    order (first-come, first-served — the early window is short).
        probe = self.probe_mode()
        for e in batch["events"]:
            if e["event"] != "TradeEvent":
                continue
            if slots_open <= 0 or len(orders) >= max_orders:
                break
            mint = e["mint"]
            coin = self.coins.get(mint)
            if coin is None or coin.complete:
                continue
            if mint in state.positions or mint in exiting:
                continue
            # A buy already in flight on this mint: do not stack another.
            if any(p.order["side"] == "buy" and p.order["mint"] == mint
                   for p in state.pending.values()):
                continue
            if coin.entries >= MAX_REENTRIES:
                continue
            if not self._qualifies(coin, probe):
                continue
            cap = state.max_buy_lamports(mint)
            if cap < MIN_BUY:
                continue
            amount = FIRST_BUY
            if amount > cap:
                amount = cap
            if free < amount + tx:
                continue
            orders.append({
                "side": "buy", "mint": mint, "quote_lamports": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            free -= amount + tx
            slots_open -= 1
            coin.entries += 1
        return orders

    def _qualifies(self, coin, probe):
        """True when the coin's early flow passes the quality bars."""
        if probe:
            max_trades = PROBE_MAX_EARLY_TRADES
            min_buy = PROBE_MIN_EARLY_BUY_SOL
            ratio_num = PROBE_MIN_BUY_SELL_NUM
            ratio_den = PROBE_MIN_BUY_SELL_DEN
            min_buyers = PROBE_MIN_BUYERS
            max_share_bps = PROBE_MAX_TOP_BUYER_SHARE_BPS
            cb_min = PROBE_CREATOR_BUY_MIN
            cb_max = PROBE_CREATOR_BUY_MAX
        else:
            max_trades = MAX_EARLY_TRADES
            min_buy = MIN_EARLY_BUY_SOL
            ratio_num = MIN_BUY_SELL_NUM
            ratio_den = MIN_BUY_SELL_DEN
            min_buyers = MIN_BUYERS
            max_share_bps = MAX_TOP_BUYER_SHARE_BPS
            cb_min = CREATOR_BUY_MIN
            cb_max = CREATOR_BUY_MAX
        if coin.trade_count > max_trades:
            return False
        if coin.cum_buy_sol < min_buy:
            return False
        if coin.cum_buy_sol * ratio_den < coin.cum_sell_sol * ratio_num:
            return False
        if len(coin.buyers) < min_buyers:
            return False
        if coin.top_buyer_share_bps() > max_share_bps:
            return False
        if coin.creator_buy_sol < cb_min or coin.creator_buy_sol > cb_max:
            return False
        if coin.creator_sold:
            return False
        return True


def main():
    bot = Bot()
    makefun_bot.run(bot.on_batch)


if __name__ == "__main__":
    main()
