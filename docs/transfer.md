# Transfer Hermes data — phone ↔ PC

Three ways to move `~/.hermes` to `~/.ophelia` without USB cables.

## Option A — Free temp cloud (any network) **recommended**

Works over mobile data / different Wi-Fi. Uses [transfer.sh](https://transfer.sh) — free HTTPS link, ~14 days, no account.

### Phone (Termux)

```bash
pip install -e ~/Ophelia   # or your clone path
ophelia transfer cloud-upload
```

Copy the URL it prints (e.g. `https://transfer.sh/xxxxx/ophelia-hermes-bundle.tar.gz`).

Send that link to your PC (Telegram yourself, email, Discord, etc.).

### PC (Windows)

```powershell
ophelia transfer cloud-download "https://transfer.sh/....../ophelia-hermes-bundle.tar.gz"
```

Imports automatically: SOUL, memories, OAuth, `state.db`, skills.

Then:

```powershell
ophelia auth refresh
ophelia doctor --chat-only
ophelia ui
```

---

## Option B — Same Wi-Fi (direct, no third party)

### PC — start receiver

```powershell
ophelia transfer receive
```

Prints something like:

```
URL:   http://192.168.1.42:8777
Token: xYz123...
```

Allow Windows Firewall for Python if prompted.

### Phone — upload

```bash
ophelia transfer send http://192.168.1.42:8777 --token xYz123...
```

PC auto-imports when upload finishes.

---

## Option C — Manual bundle (USB / Drive)

```bash
# Phone
bash scripts/termux-export-hermes.sh
```

Copy `ophelia-hermes-bundle.tar.gz` to PC, then:

```powershell
tar -xzf ophelia-hermes-bundle.tar.gz
ophelia migrate hermes --source .\hermes
ophelia auth import-hermes --hermes-home .\hermes
```

---

## What gets transferred

| Data | Purpose |
|------|---------|
| `SOUL.md` | Personality |
| `memories/` | Long-term MEMORY + USER |
| `auth.json` | SuperGrok OAuth |
| `state.db` | Search old Hermes chats |
| `skills/`, `honcho.json` | Optional extras |

**Not included:** Ophelia's live `memory.db` (starts fresh on PC).

---

## Security notes

- **Cloud links are public** — anyone with the URL can download until expiry. Use soon; don't post publicly.
- **Direct Wi-Fi** uses a one-time token; stop the receiver after import.
- Auth tokens end up in `~/.ophelia/` — never commit to GitHub.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `transfer.sh` blocked / slow | Use Option B (same Wi-Fi) or Option C (USB) |
| Firewall blocks receive | Allow Python on private network |
| OAuth fails after import | `ophelia auth refresh` or re-export `auth.json` from phone |
| Phone can't reach PC IP | Same Wi-Fi? Try cloud-upload instead |
