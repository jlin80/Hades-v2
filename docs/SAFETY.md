# Safety

Hades V2 is **paper trading only**. Not by configuration — by construction.

## What is absent

There is no private key handling, no signer, no wallet, no transaction builder and no
RPC submission path anywhere in `src/`. Not disabled behind a flag: absent.

`trading_mode` and `is_live` in the API payload are typed as `Literal["paper"]` and
`Literal[False]`. They are not settings. There is no other value they can take,
because there is no other code path for them to describe.

## How it is enforced

`tests/test_safety.py` walks every `.py` file under `src/` and fails the build on:

1. **An import of a signing or submission library** — `solana`, `solders`, `anchorpy`,
   `nacl`, `ecdsa`, `eth_account`, `bip_utils`, `mnemonic`. AST-based, so it catches
   `import x`, `from x import y` and aliased forms alike.
2. **A key-material identifier** — `private_key`, `secret_key`, `keypair`,
   `sign_transaction`, `send_transaction`, `wallet_signer`, `seed_phrase`.

A third test asserts the scan actually found files. Without it, a broken path
expression would make both checks pass vacuously — a green build proving nothing.

## Why a test and not a rule

A documented constraint decays at the speed of the people who remember it. Hades V1's
structural isolation of the Research Lab held for months precisely because violating
it broke the build, and the same technique caught a duplicate event class that had
been silently overwriting a governance event in the registry.

This scan will eventually be inconvenient — Phase 1 will want to talk to Solana RPC
for read-only data. When that happens, the correct move is to **narrow the rule to the
capability**, not to delete it: reading `getAccountInfo` over HTTP needs no signing
library at all, and if a candidate provider SDK insists on pulling one in, that is a
finding about the SDK.

## Live trading

Out of scope until the system has demonstrated, on real collected data, a positive
expectancy after slippage, fees and risk. That is the whole point of the build. Until
then, adding a live path adds risk against an edge that does not yet exist.
