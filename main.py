"""Pump.fun Replay Sprint — paper trading bot (pure Python stdlib).

Strategy: TIERED DIP REVERSION. Buy the dip — but only the dips that
screened positive. Offline screening over the dev window (faithful fill
model: 2-slot latency, 1500 bps slippage rejection, 1% size cap, real
fees) tested ~45 rules across the momentum, sniper, dip, exhaustion,
late-ride, completion-ride, smart-wallet, serial-creator, revival,
accumulation and volume-breakout families. Almost everything loses in
at least one half of the window. The one surviving shape is a deep dip
on a coin whose creator has skin in the game:

  - price down to 70% or less of its 40-trade high,
  - buyers stepping back in (last 10 trades net buys, >= 0.2 SOL),
  - dispersed ownership (7+ distinct buyers in the last 40 trades, no
    single buyer over 35% of the buys),
  - creator bought 0.05-1 SOL of their own coin and has not sold.

That primary lane screened +0.334 SOL on 20 fills (5 coins, win 0.65)
with a positive total in BOTH halves of the window — the only rule in
the whole screen with that property at meaningful size. Two cheaper
tiers fill out the trade count so the 20-coin guardrail stays in reach:
a -20% probe tier (same creator + resumption gates, 0.08 SOL) and a
-10% filler tier (0.02 SOL, fired only when round trips lag the pace
of the window). Every tier exits the same way: take-profit +15%,
stop-loss -12%, a trailing stop that arms at +5% and sells on a 5%
pullback from the peak, a 1800-slot hold cap, 900-slot staleness, or a
creator sell. Ablations that shaped this: dropping the creator filter
turns the dip edge deeply negative; widening the buy slippage hurts;
uncapping the take-profit hurts; pyramiding hurts; late-curve
completion bets lose (stop fills fail on fast dumps).

Why the edge should generalize (and its limits): the rule does not
chase pumps — it waits for a 30% washout, then demands evidence that
real, dispersed buyers are returning while the creator still holds
their stake. That is a statement about market structure (washout +
re-accumulation), not about any one coin's chart. The honest limit is
concentration: the primary lane fired on only 5 coins in the dev
window, so a fresh window's outcome depends on whether such coins show
up again — the stress test is the arbiter, and the probe/filler tiers
exist precisely so the bot still trades broadly if they do not.

Protocol: the harness speaks agent-fun/replay/v2 over stdio — one hello,
then one batch per slot carrying events, marks and fills, then done. This
file owns the strategy only; vendor/makefun_bot.py owns the wire. Every
decision here is a pure function of the batches seen so far: no clock,
no randomness, no network, no environment reads, and every iteration
that affects order is over insertion-ordered dicts, an explicit sort,
or batch order, so two graded runs produce byte-identical action logs.
All price comparisons are integer cross-multiplication on reserve
rationals; there are no float literals anywhere in this file.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

SOL = 1_000_000_000

# --- sizing (lamports) -------------------------------------------------------
SIZE_PRIMARY = 150_000_000   # 0.15 SOL: deep-dip entries on mid-curve coins
SIZE_PROBE = 80_000_000      # 0.08 SOL: -20% probe tier
SIZE_FILLER = 20_000_000     # 0.02 SOL: -10% filler tier (2x the round-trip bar)
MIN_BUY = 10_000_000         # 0.01 SOL: below this a round trip is no good
MAX_SLIPPAGE_BPS = 1500

# --- dip entry bars (shared by all tiers unless noted) -----------------------
MIN_AGE_SLOTS = 150          # coin must be at least this old (slots)
MIN_TRADES = 41              # ...and have at least this many trades seen
                             # (41: the 40-trade high needs 41 points)
HIGH_WINDOW = 41             # 40-trade high, including the current trade
FLOW_WINDOW = 40             # buyer window, including the current trade
RESUME_WINDOW = 10           # resumption window, including the current trade
MIN_RESUME_BUY_SOL = 200_000_000  # last-10-trades buys must clear this
MIN_BUYERS = 7               # distinct buyers in the buyer window (primary)
MAX_TOP_BUYER_SHARE_BPS = 3500   # no single buyer over 35% (primary)
CREATOR_BUY_MIN = 50_000_000     # creator skin in the game: 0.05 SOL
CREATOR_BUY_MAX = 1_000_000_000  # ...but over 1 SOL smells bundled
# Depth tiers, as integer ratios of current price to the 40-trade high:
# primary fires at <= 70%, probe at <= 80%, filler at <= 90%.
PRIMARY_NUM = 70
PROBE_NUM = 80
FILLER_NUM = 90
DEPTH_DEN = 100

# --- exits (all comparisons in integer basis points; no floats anywhere) -----
TAKE_PROFIT_BPS = 11500         # +15%
STOP_LOSS_BPS = 8800            # -12%
TRAIL_ARM_BPS = 10500           # trailing stop arms once peak reaches this
TRAIL_FACTOR_BPS = 9500         # sell when value falls to 95% of peak
MAX_HOLD_SLOTS = 1800           # ~12 min
STALE_SLOTS = 900               # no trade for this long -> exit

MAX_TRADES_KEPT = 64
PRUNE_AFTER_SLOTS = 4000        # drop idle unheld coins this stale
MAX_ENTRIES_PER_TIER = 4


class Coin:
    """Per-mint flow state, built only from the batches the harness sends."""

    __slots__ = (
        "mint", "created_slot", "creator", "complete", "last_trade_slot",
        "trade_count", "trades", "creator_buy_sol", "creator_sold",
        "entries_primary", "entries_probe", "entries_filler",
    )

    def __init__(self, mint, created_slot, creator):
        self.mint = mint
        self.created_slot = created_slot
        self.creator = creator
        self.complete = False
        self.last_trade_slot = created_slot if created_slot is not None else -1
        self.trade_count = 0
        # (slot, is_buy, sol_lamports, user, vsol, vtok), oldest first
        self.trades = []
        self.creator_buy_sol = 0
        self.creator_sold = False
        self.entries_primary = 0
        self.entries_probe = 0
        self.entries_filler = 0

    def note_trade(self, slot, is_buy, sol, user, vsol, vtok):
        self.last_trade_slot = slot
        self.trade_count += 1
        self.trades.append((slot, is_buy, sol, user, vsol, vtok))
        if len(self.trades) > MAX_TRADES_KEPT:
            del self.trades[0]
        if is_buy:
            if user == self.creator:
                self.creator_buy_sol += sol
        else:
            if user == self.creator:
                self.creator_sold = True

    def _price_le_ratio(self, vsol, vtok, hsol, htok, num, den):
        """True when vsol/vtok <= (num/den) * (hsol/htok), integer math."""
        return vsol * htok * den <= hsol * vtok * num

    def tier(self):
        """Dip tier of the latest trade: 30 (primary), 20 (probe),
        15 (filler) or 0 (no dip / gates failed). Uses only trades seen
        so far; mirrors the screened rule exactly."""
        if self.created_slot is None:
            return 0
        if self.trade_count < MIN_TRADES:
            return 0
        if self.last_trade_slot - self.created_slot < MIN_AGE_SLOTS:
            return 0
        if self.creator_sold:
            return 0
        if (self.creator_buy_sol < CREATOR_BUY_MIN
                or self.creator_buy_sol > CREATOR_BUY_MAX):
            return 0
        trades = self.trades
        # 40-trade high over the last HIGH_WINDOW trades (incl. current).
        hi = trades[-HIGH_WINDOW:]
        hsol, htok = hi[0][4], hi[0][5]
        for t in hi[1:]:
            # t's price > current high? t.vsol/t.vtok > hsol/htok
            if t[4] * htok > hsol * t[5]:
                hsol, htok = t[4], t[5]
        if hsol <= 0 or htok <= 0:
            return 0
        cur = trades[-1]
        vsol, vtok = cur[4], cur[5]
        if vsol <= 0 or vtok <= 0:
            return 0
        if not self._price_le_ratio(vsol, vtok, hsol, htok, FILLER_NUM, DEPTH_DEN):
            return 0
        # Resumption: last RESUME_WINDOW trades net buys >= the bar.
        buy = 0
        sell = 0
        for t in trades[-RESUME_WINDOW:]:
            if t[1]:
                buy += t[2]
            else:
                sell += t[2]
        if buy < MIN_RESUME_BUY_SOL or buy < sell:
            return 0
        if self._price_le_ratio(vsol, vtok, hsol, htok, PRIMARY_NUM, DEPTH_DEN):
            # Primary tier: dispersed ownership gates on the buyer window.
            buyers = {}
            total = 0
            for t in trades[-FLOW_WINDOW:]:
                if t[1]:
                    total += t[2]
                    buyers[t[3]] = buyers.get(t[3], 0) + t[2]
            if len(buyers) < MIN_BUYERS:
                return 0
            top = 0
            for v in buyers.values():
                if v > top:
                    top = v
            if total <= 0 or top * 10000 > total * MAX_TOP_BUYER_SHARE_BPS:
                return 0
            return 30
        if self._price_le_ratio(vsol, vtok, hsol, htok, PROBE_NUM, DEPTH_DEN):
            return 20
        return 15


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

    def filler_on(self):
        """True once the window is underway and the 20-coin guardrail is
        still unmet: the -10% filler tier fires only then, so its
        (roughly breakeven) trades buy guardrail coverage instead of
        crowding out the edge. The gate stays open until the guardrail IS
        met — switching the filler off at 15 round trips (as the first
        draft did) strands the bot 5 coins short of the bar."""
        if self.round_trips() >= 20:
            return False
        return self.batches > 1500

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

        # 2) Fresh entries, in tier-priority passes over this batch's
        #    trades: the primary lane never loses a position slot to a
        #    filler. Each pass is first-come, first-served in batch order.
        trade_events = [e for e in batch["events"] if e["event"] == "TradeEvent"]
        filler = self.filler_on()
        for want_tier, size in ((30, SIZE_PRIMARY), (20, SIZE_PROBE),
                                (15, SIZE_FILLER)):
            if want_tier == 15 and not filler:
                continue
            for e in trade_events:
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
                tier = coin.tier()
                if tier != want_tier:
                    continue
                if want_tier == 30:
                    if coin.entries_primary >= MAX_ENTRIES_PER_TIER:
                        continue
                elif want_tier == 20:
                    if coin.entries_probe >= MAX_ENTRIES_PER_TIER:
                        continue
                else:
                    if coin.entries_filler >= MAX_ENTRIES_PER_TIER:
                        continue
                cap = state.max_buy_lamports(mint)
                if cap < MIN_BUY:
                    continue
                amount = size
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
                if want_tier == 30:
                    coin.entries_primary += 1
                elif want_tier == 20:
                    coin.entries_probe += 1
                else:
                    coin.entries_filler += 1
        return orders


def main():
    bot = Bot()
    makefun_bot.run(bot.on_batch)


if __name__ == "__main__":
    main()
