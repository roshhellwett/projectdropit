[![python](https://img.shields.io/badge/python-3.8%2B-blue)](#)
[![license](https://img.shields.io/badge/license-MIT-green)](#)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](#)

# PROJECTDROPIT

> A menu-driven peer-to-peer encrypted file transfer CLI for your local network.

- 🛰  **Auto-discovery** on the LAN (zeroconf / mDNS)
- 🔒 **End-to-end encryption** every session — X25519 ECDH + AES-256-GCM
- ✅ **SHA-256 integrity verification** on every transfer
- ⚡ **Zero setup** — no internet, no cloud, no accounts, no admin/root, no pre-shared passwords
- ✨ **Premium TUI** — rich panels, live discovery, progress, speed, ETA
- 🔄 **Auto-update check** on launch (via PyPI, non-blocking)
- 🐧 **Linux** + 🪟 **Windows** + 🍎 **macOS**, Python 3.8+

---

## Install

```bash
pip install projectdropit
```

Or from source:

```bash
pip install .
```

## Run

```bash
projectdropit -start
```

(`projectdropit`, `projectdropit start`, and `projectdropit --start` all work too.)

On first launch you must enter a device name — that's how others on the LAN
will see you. Settings can be changed later from the menu.

## Menu

```
1. Discover Devices     — live list of online peers on your LAN
2. Send a File          — pick a peer, pick a file, transfer starts
3. Incoming Transfers   — accept / reject offers, view history
4. Settings             — name, download folder, update check, about
5. Quit
```

Received files land in `~/projectdropit_files/` by default.

## How it works

- **Discovery**: zeroconf broadcasts `<device-name>._projectdropit._tcp.local.`
  with your TCP port and a `name` TXT record.
- **Transport**: a fresh TCP socket per session.
- **Handshake**: X25519 ECDH → HKDF-SHA256 → 32-byte AES key.
- **Stream**: each frame is `[12-byte nonce][4-byte length][AES-256-GCM ciphertext]`.
- **Integrity**: sender streams a SHA-256 of the file; receiver computes its own
  and compares before marking the transfer complete. Mismatched files are
  deleted and the transfer fails loudly.

## Windows firewall

The first time you run `projectdropit`, Windows shows the standard
"Allow an app to communicate" dialog. Click **Allow** on private networks.
**No admin password is ever required.** The app prints a friendly heads-up
before opening its socket.

## CLI flags

| Flag | Purpose |
|---|---|
| `-start` / `--start` / `start` | launch the menu (default) |
| `--no-update` / `-no-update` | skip update check on launch |
| `-v` / `--version` / `version` | print version and exit |
| `-h` / `--help` / `help` | show usage |

## Non-goals

No internet transfers. No accounts. No GUI. No admin requirement.
No pre-shared passwords. No cloud.

---

© 2026 [Zenith Open Source Projects](https://zenithopensourceprojects.vercel.app/). All Rights Reserved. Zenith is a Open Source Project Idea's by @roshhellwett
