# Pump.fun Replay Sprint — Paper Bot, Real Ranking

[![make.fun](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/RieryBuild/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/RieryBuild/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper trading bot for the [make.fun](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) Pump.fun Replay Sprint. It trades 10 SOL of paper money on a 24h replay of pump.fun bonding-curve activity.

## Strategy

Buy quality launches early, take the profit, never chase:

- **Enter only in a coin's first 30 trades**, when its early flow is buy-heavy (cumulative buys >= 3 SOL and at least twice cumulative sells), dispersed (7+ distinct buyers, no single buyer over 35% of the buys), and honest (the creator put in 0.05-1 SOL of their own money and has not sold).
- **One entry per coin** (up to four entries while it stays in its early window), 0.08 SOL sized down by the 100 bps-of-real-reserve size cap, at most 5 open positions.
- **Whole-position exits.** Take profit at +25%, stop-loss at -15%, a tight trailing stop that arms at +5% and sells on a 5% pullback from the peak, a ~48-minute hold cap, staleness, or a creator sell — whichever comes first.
- **Guardrail probes.** If round-tripped coins lag the pace of the window, the entry bars relax (first 60 trades, 1 SOL, 5 buyers) so the 20-distinct-coin bar stays in reach.

The bars come from offline screening over the dev window with latency, the size cap, slippage rejections and fees all modelled: chasing mature momentum loses money, riding winners uncapped loses money, and the creator-skin + dispersed-ownership filters are what turn the early-entry edge positive. The 15% slippage bound is kept on purpose — widening it lets the bot fill into pumps that have already run away.

## Running

Pure Python 3.13 standard library — nothing to install. The harness runs it as `python3 -P -s main.py` and speaks the `agent-fun/replay/v2` protocol over stdio (see `vendor/makefun_bot.py`, the kit SDK vendored byte-identical). The bot is fully deterministic: no clock, no randomness, no network, no environment reads — two runs produce byte-identical action logs.

## Layout

- `main.py` — the bot (entrypoint)
- `vendor/makefun_bot.py` — the kit's Python SDK, vendored as its docstring instructs
- `.makefun/owner.json` — entry wallet claim

## License

MIT — see [LICENSE](LICENSE).
