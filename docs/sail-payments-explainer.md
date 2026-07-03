# How payments work on your SAIL host

This explains, in operator terms, how your host earns sats and how the built-in wallet works:
**pay-to-open channels, the fees involved, your recovery seed, and how to receive, withdraw, and
close/sweep** — plus the security boundary around the wallet. It assumes the recommended
**phoenixd** payout backend (self-custodial). If you run your own LND or a connected wallet (NWC),
you manage funds in your own node/wallet and most of this doesn't apply.

> Fee figures here are **rough ranges**, not promises — Lightning and on-chain fees move with
> market conditions. For exact current numbers, see **ACINQ's official Phoenix fee schedule:**
> <https://phoenix.acinq.co/faq> (the "fees" section).

---

## The short version

- Clients pay your host **per token of output**, over Lightning, as they use it.
- phoenixd is a real, **self-custodial** Lightning node running on your machine. You hold the keys
  (a 12-word seed). SAIL never sees or holds your funds.
- The **first** payment your node receives has to be large enough to **open a Lightning channel**
  (~25–35k sat) — that's a one-time cost, after which per-token payments flow normally.
- You move sats with three buttons in the dashboard wallet card: **Receive / fund**, **Withdraw**,
  and **Close & sweep**.
- The wallet (balance, seed, withdraw, close) is reachable **only from this machine** — never over
  your `.onion`.

---

## How you earn

A client discovers your host, then pays **per output token** using **L402** (a Lightning-native
HTTP 402 flow): your host issues a small invoice, the client pays it, your host streams the next
chunk of tokens, and so on. Payments are **metered** — a client pays for tokens actually delivered,
not a flat fee. phoenixd receives those payments and credits your balance.

Your earnings accumulate in your phoenixd node. The dashboard wallet card shows your **spendable
balance** and channel status, updating as payments arrive.

---

## Channels & pay-to-open (the "channel cliff")

phoenixd uses **pay-to-open / automatic channels** — there is **no manual "open a channel" step**.
A channel opens by itself when an inbound payment large enough to cover the channel-open cost
arrives (**~25–35k sat** at typical fees).

- **Before your first channel:** your node can't receive normal payments yet. A small incoming
  payment is held as **fee credit** (shown separately from spendable balance) rather than opening a
  channel. This is the "channel cliff."
- **Crossing the cliff:** receive one payment of **~25–35k+ sat** (use **Receive / fund** to make
  an invoice and pay it from another wallet, or let your first real customer's payment do it). That
  payment **auto-opens your channel**, minus the open fee.
- **After that:** per-token micro-payments (a few sats each) flow normally, and your spendable
  balance grows.

A brand-new channel may have **limited inbound** (room to receive). A large incoming payment can
trigger another pay-to-open (another fee). Small payments are unaffected.

---

## Fees (ranges, not promises)

You'll encounter three kinds of fee. **For exact current figures, see ACINQ's fee schedule:**
<https://phoenix.acinq.co/faq>.

- **Pay-to-open / liquidity fee** — paid once when a channel is opened (your first ~25–35k sat
  payment), and again if a later large payment needs more inbound liquidity. It covers the on-chain
  cost of the channel plus ACINQ's liquidity service. Roughly **~1% + the on-chain mining cost** of
  the funding, but it varies with on-chain fees and the amount.
- **Routing fee (withdraw)** — when you **withdraw** over Lightning, you pay the network's routing
  fee to get the payment to its destination. Usually **tiny** (often a few sats to ~0.x%), set by
  the route.
- **On-chain fee (close)** — when you **close & sweep**, the closing transaction pays a Bitcoin
  **mining fee** (feerate × transaction size). On a small balance this can eat most or all of it —
  see the dust warning below.

SAIL itself takes **no cut** of your payments — these are Lightning/Bitcoin network and liquidity
fees, not SAIL fees.

---

## Your recovery seed — back it up

phoenixd is **self-custodial**: your wallet is controlled by a **12-word recovery phrase** (BIP39),
generated on first run and stored only on your machine (`~/.phoenix/seed.dat`, owner-readable).

- **Anyone with these 12 words controls your funds.** Write them on paper, store them offline, and
  never photograph them, paste them into a website, or save them digitally.
- **SAIL never sees or transmits your seed** — not to relays, not to any SAIL service, not to logs.
  "Back up wallet" reads it locally for you to copy down, then re-hides it.
- If you lose the seed **and** lose the machine, the funds are gone. If you keep the seed, you can
  **restore** the wallet on a new machine (the wizard's "Import existing seed").

---

## Moving your sats

All three live in the dashboard wallet card and run against your own node:

- **Receive / fund** — generates a Lightning invoice (shown as text + QR). Paying it credits your
  wallet; the first sufficient payment auto-opens your channel. Use this to bootstrap the channel or
  to top up.
- **Withdraw** — pay an external Lightning invoice to move sats **out** over Lightning (fast, cheap).
  Paste a BOLT11; if it has a fixed amount the field fills automatically. This is the normal way to
  take profits.
- **Close & sweep** — closes your channel and sends the remaining balance **on-chain** to a Bitcoin
  address you provide. This pays a mining fee and **ends the channel** (receiving again later needs a
  new channel, i.e. another pay-to-open). **Withdraw over Lightning first** when you can — close is
  for emptying the on-chain remainder or shutting the host down.

> **Don't close into dust.** On a small balance the on-chain close fee can leave you with little or
> nothing. The close screen shows the estimated fee and the **net** you'd receive before you confirm,
> and warns when your balance is at or below the close cost — in that case, withdraw over Lightning
> instead.

---

## The local-only wallet boundary

Your host serves inference to the world over a Tor **`.onion`** address. The **wallet and operator
surface do not.** They run on a **separate localhost-only port** (default `8092`) that is **never
added to the Tor hidden service**, so the dashboard, the wizard, and every wallet action —
**balance, seed reveal, withdraw, close** — are reachable **only from the machine itself**, never
over the `.onion`, regardless of any request header.

In short: the public can pay you, but only you (on this machine) can see or move the money.

---

*Exact fees change with market conditions — always check ACINQ's current schedule at
<https://phoenix.acinq.co/faq> before a large action.*
