# Pulling the Google Photos OAuth token off a rooted device via ADB

`--adb-token` reads the short-lived `photos.native` OAuth bearer that GMS caches on a
rooted Android device signed into the target Google account, and injects it into gpmc
so you skip the full `auth_data` master-token exchange. `gpmc_adb_auth.py` does all of
this; this file is for when it isn't working yet or you need to understand it.

## Prerequisites

- The device is **rooted** (`su` works over adb) and **authorized for adb** (`adb devices`
  shows it as `device`, not `unauthorized`).
- It's signed into the **target** Google account in the stock Google Photos app.
- `adb` is on PATH (it's in the `nix develop` shell here).

## The happy path

Usually you don't touch any of this — just run the migration with `--adb-token`. To pull
the token by hand (e.g. to sanity-check auth), the module is runnable:

```bash
python gpmc_adb_auth.py                 # auto-detects the account, prints the ya29.* token
python gpmc_adb_auth.py --serial <S>    # if multiple devices are attached
```

Under the hood it reads `accounts_ce.db` as root. The two queries it runs (SQL is piped
over stdin to avoid nested-quote hell through adb + the device shell):

```bash
# find the Google account's row id + email
printf "SELECT _id, name FROM accounts WHERE type='com.google';\n" \
  | adb shell "su -c 'sqlite3 /data/system_ce/0/accounts_ce.db'"

# pull the cached photos.native bearer for that account id (e.g. 1)
printf "SELECT authtoken FROM authtokens WHERE accounts_id=1 AND type LIKE '%%photos.native%%';\n" \
  | adb shell "su -c 'sqlite3 /data/system_ce/0/accounts_ce.db'"
```

A good token starts with `ya29.` and is a few hundred characters. Confirm it works by
constructing a client and hitting a read-only endpoint (`get_media_key_by_hash` on a
random hash returns `None` when authenticated, raises on a bad token) — see how
`attach_adb_auth` seeds `client.api.auth_response_cache` in `gpmc_adb_auth.py`.

## When the token is missing or stale

The cache only holds a token if GMS has minted one, and it expires ~1h:

- **No `photos.native` row / empty result:** open Google Photos on the phone once so GMS
  mints the token, then re-pull.
- **First call 401s / token is stale:** same fix — open Photos (or wait) to refresh, then
  re-pull. During a migration the run pauses on 401 and waits for Enter precisely so you
  can do this without losing progress.
- **Wrong account:** `accounts_ce.db` may hold several accounts; pass `--adb-account-id`
  (the `_id` from the first query) or `--adb-serial` for the right device.

## Dead-ends — do NOT waste time here

These were tried on a stock-GMS, Android-16, rooted device and don't pay off; the
`accounts_ce.db` read above is the reliable route:

- **Hooking `SSL_write` with frida to sniff the auth request.** Stock GMS ships cronet
  and conscrypt with **fully stripped TLS symbols** (no `SSL_write` by export or symtab),
  so there's nothing to hook by address. frida 17 also dropped the built-in `Java` bridge,
  so a Java-layer Conscrypt hook needs a compiled agent bundling `frida-java-bridge`.
- **MITM proxy (mitmproxy / HTTP Toolkit) for the `/auth` body.** On Android 14+ the CA
  store moved into the read-only `com.android.conscrypt` APEX, and GMS pins its certs — so
  you'd need both an APEX-CA bind-mount and a frida unpinning script just to read one
  request. Not worth it when the token is already sitting in `accounts_ce.db`.
- **Reading the master token from `accounts.password`.** Empty on modern GMS (Android 16) —
  the durable master token isn't stored there anymore, only the short-lived OAuth token in
  `authtokens`. That's why this flow uses the short-lived token + refetch, not `auth_data`.

## Handling the credential responsibly

The bearer grants that one account's Photos access until it expires. Keep it in a local
file / env var, never transmit it anywhere, and if the account isn't the user's own,
confirm they're authorized to extract it (e.g. migrating a relative's library with consent)
before pulling.
