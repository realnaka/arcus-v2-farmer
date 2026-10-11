# Disclaimer

**This repository is published for research and study. It is not investment
advice, and it is not a solicitation to trade.**

Trading perpetual futures can liquidate an entire account.  The strategies and
parameters here are one operator's snapshot of one venue's rules at one point
in time; they were derived from a small sample of public data and from the
venue's own weekly aggregates.  Treat every efficiency figure as an order of
magnitude, not a formula.

In particular:

- **Venue rules are discretionary.**  Point programs, fee tiers, margin
  requirements and the set of tradable markets can change without notice, and
  changes can be applied retroactively.
- **Wash trading, self-matching, deliberate order clustering and rapid
  deposit-trade-withdraw cycling are penalised** by the venue's own published
  rules.  This code is written to look like a position rather than a churn
  loop, but that is a design intent, **not a compliance guarantee**.
- **You are responsible for compliance** with the terms of any venue you
  connect this code to, and with the laws and regulations of your
  jurisdiction.  Some venues exclude residents of the United States, the
  United Kingdom, Canada and other regions.
- **Run your own keys.**  Never commit private keys, mnemonics, API keys,
  alert webhooks or account state.  The repository ships placeholders only.

If any of that is unacceptable to you, do not use this code.
