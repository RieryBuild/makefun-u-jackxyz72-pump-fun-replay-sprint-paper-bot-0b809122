# Pump.fun Replay Sprint — Paper Bot, Real Ranking

[![make.fun](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/RieryBuild/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/RieryBuild/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper trading bot for the [make.fun](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) Pump.fun Replay Sprint. It trades 10 SOL of paper money on a 24h replay of pump.fun bonding-curve activity.

## Strategy

Tiered dip reversion — buy the dip, but only the dips that screened positive:

- **Primary lane (0.15 SOL).** Price down to 70% or less of its 40-trade high, buyers stepping back in (last 10 trades net buys of at least 0.2 SOL), dispersed ownership (7+ distinct buyers in the last 40 trades, no single buyer over 35%), and the creator holding 0.05–1 SOL of skin in the game without having sold. This was the only rule in an offline screen of ~45 candidates that finished positive in both halves of the dev window.
- **Probe lane (0.08 SOL).** The same creator + resumption gates at a shallower dip (down to 80% of the 40-trade high).
- **Filler lane (0.02 SOL).** An even shallower dip (down to 90%) that fires only when round-tripped coins lag the pace of the window, so the 20-distinct-coin guardrail stays in reach without crowding out the edge.
- **Whole-position exits.** Take profit at +15%, stop-loss at −12%, a trailing stop that arms at +5% and sells on a 5% pullback from the peak, a 1800-slot hold cap, staleness, or a creator sell — whichever comes first.
- **At most 5 open positions**, entries capped at 100 bps of real reserve, 15% slippage bound kept on purpose (widening it lets the bot fill into pumps that have already run away).

The bars come from offline screening over the dev window with latency, the size cap, slippage rejections and fees all modelled. Ablations that shaped the design: dropping the creator filter turns the dip edge deeply negative; uncapping the take-profit hurts; pyramiding hurts; late-curve completion bets lose because stop fills fail on fast dumps. Honest limit: the primary lane fired on only 5 coins in the dev window, so the result is concentrated — the stress test, not the dev score, is the arbiter.

## Running

Pure Python 3.13 standard library — nothing to install. The harness runs it as `python3 -P -s main.py` and speaks the `agent-fun/replay/v2` protocol over stdio (see `vendor/makefun_bot.py`, the kit SDK vendored byte-identical). The bot is fully deterministic: no clock, no randomness, no network, no environment reads, integer-only math — two runs produce byte-identical action logs.

## Layout

- `main.py` — the bot (entrypoint)
- `vendor/makefun_bot.py` — the kit's Python SDK, vendored as its docstring instructs
- `test_main.py` — unit tests for the strategy
- `.makefun/owner.json` — entry wallet claim

## License

MIT — see [LICENSE](LICENSE).
