# kraken-cli Skills

49 goal-oriented skill packages for AI agents that operate `kraken-cli`.

See the full [Skills Index](INDEX.md) for the complete categorized list.

## Skill Types

- **Core** (5): Shared contract, autonomy levels, rate limits, order types, error recovery
- **Market Data** (4): Price reads, multi-pair screening, alerts, WebSocket streaming
- **Spot Trading** (6): Order execution, stops, fees, paper testing, paper-to-live promotion, risk controls
- **Futures** (5): Trading lifecycle, risk management, liquidation guard, basis trades, funding carry
- **Strategies** (4): DCA, grid trading, rebalancing, TWAP execution
- **Funding & Earn** (3): Deposits/withdrawals, staking, tax exports
- **Portfolio & Account** (2): Balance analysis, subaccount management
- **Recipes** (20): Multi-step workflows combining multiple skills

## OpenClaw Setup

From the repository root, symlink all skills:

```bash
cd /path/to/kraken-cli
for skill in skills/kraken-* skills/recipe-*; do
  ln -s "$(pwd)/$skill" ~/.openclaw/skills/
done
```

Install specific skills:

```bash
cp -r skills/kraken-spot-execution ~/.openclaw/skills/
cp -r skills/recipe-start-dca-bot ~/.openclaw/skills/
```
