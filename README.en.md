# WhatsApp → Telegram

🇧🇷 [Leia em português](README.md)

Imports an exported WhatsApp chat (private or group) into Telegram through the **official
history import API** (`messages.initHistoryImport`), the same one the mobile apps use.
Messages keep their original date and time and are marked as imported.

It has two advantages over importing from your phone: it handles large exports (several GB),
and it resumes where it stopped if the connection drops.

> The script's messages and prompts are in Portuguese. This guide explains each step.

## Requirements

- Python 3.9+
- `bsdtar` (ships with macOS; on Linux, install `libarchive-tools`). Without it the script
  falls back to Python's `zipfile`, which loses files in WhatsApp exports larger than 4 GB
  (see below).
- A Telegram API ID and API hash: create them at <https://my.telegram.org> → *API development tools*.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Step by step

1. **Export the chat in WhatsApp:** open the chat → contact/group name → *Export chat* →
   *Attach media*. Save the `.zip` to your computer.
2. **Check what is already in Telegram.** If this chat was imported before, importing it again
   duplicates the messages. The script warns you when the period is already listed in
   `historico_importacoes.json`, but it only knows about imports it made itself. If in doubt,
   export the chat from Telegram Desktop (*Settings → Advanced → Export Telegram data*) and compare.
3. **For groups:** the destination must be a **supergroup** with every member already added.
   The script converts a basic group with `--migrar-grupo` (the ID changes).
4. **Find the destination ID:**
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --listar-chats
   ```
   The first time it asks for your API ID, API hash, phone number and the login code. The
   code arrives in the Telegram app.
5. **Dry run** (validates the export and the destination without importing anything):
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --export "WhatsApp Chat - John.zip" --destino 123456789 --simular
   ```
6. **Import** (asks you to confirm by typing `sim`, Portuguese for "yes"):
   ```sh
   caffeinate -i .venv/bin/python whatsapp_para_telegram.py --export "WhatsApp Chat - John.zip" --destino 123456789
   ```
   `caffeinate` (macOS) keeps the Mac awake during the upload. If the connection drops or some
   media fails, run the same command again: it only sends what is missing.
7. **Log out** when you are done:
   ```sh
   .venv/bin/python whatsapp_para_telegram.py --sair
   ```

The API ID and hash can also come from the `TG_API_ID` and `TG_API_HASH` environment variables
instead of being typed.

### Options

| Option | Meaning |
|---|---|
| `--export` | WhatsApp `.zip` or an already extracted folder |
| `--destino` | destination: `@username`, `+phone` or numeric ID |
| `--listar-chats` | list your chats and their IDs |
| `--simular` | dry run: validate only, import nothing |
| `--migrar-grupo` | convert a basic group into a supergroup |
| `--paralelo N` | simultaneous uploads (default 3) |
| `--arquivo-chat` | path to the chat `.txt`, if auto-detection fails |
| `--sair` | log out from Telegram and delete the local session file |
| `--sessao` | session file path (default: next to the script) |

## How Telegram behaves

- **Where the messages show up:** if the destination chat has fewer than 1,000 messages, imported
  messages are merged into the history at their original dates. Otherwise they appear as a block
  on the day of the import, each one labeled with its original date.
- **Who sees them:** everyone in the chat.
- **There is no undo** for a whole import.
- **Size limit:** files over 2 GB only upload with Telegram Premium (up to 4 GB).

## Technical notes

- **Exports over 4 GB:** WhatsApp writes a truncated zip index. Tools that rely on the index
  (`unzip`, `zipfile`, Finder) see only part of the files and usually miss `_chat.txt`, which is
  stored last. The script extracts with `bsdtar` reading the zip sequentially through a pipe, then
  checks that every attachment mentioned in the chat (`<attached: …>`, `<anexado: …>`, etc.)
  exists in the folder.
- **Accented file names:** WhatsApp stores names as UTF-8 without setting the zip flag, so
  `zipfile` would read them as cp437. The script fixes this.
- **Extraction folder:** zips are extracted into `extraidos/` next to the script, not next to the
  zip, so extractions don't land in a synced folder (Desktop/Documents on iCloud).
- **Dates:** the export period is read as day/month/year. Exports from phones set to US format
  (month/day/year) will show wrong periods, which affects the duplicate-import warning.

## Local files (not in Git)

| File | What it is |
|---|---|
| `importador_wa.session` | an active login to your account. Don't share it; remove it with `--sair`. |
| `historico_importacoes.json` | what was imported and into which chat |
| `extraidos/` | extracted exports |
| `.importacao_telegram_*.json` | progress of an import, used to resume |

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## License

[MIT](LICENSE)
