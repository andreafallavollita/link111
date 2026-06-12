# AGENTS.md

## Scopo del repository

Sistema personale di crescita rete LinkedIn: discovery automatica di prospect coerenti con l'ICP (AI / marketing automation / martech), scoring, invio richieste con safety, dashboard. Target: 5000 connessioni.

Esiste anche un sistema legacy (script singolo `send_linkedin_requests.py`) usato in passato per liste statiche su Excel. Vedere sezione finale.

## Focus operativo

Sistema **autonomo**: l'orchestratore `run_daily.ps1` parte ogni giorno via Task Scheduler, fa discovery se la queue e' bassa, invia il quota giornaliero spalmato in fascia 9-18, scrive un report. **Non chiedere conferme operative al lancio** salvo ambiguita' reali (cap LinkedIn raggiunto, CAPTCHA persistente, prospects in queue insufficienti per riempire la quota).

## Architettura

### Componenti
- **`growth_db.py`** — schema SQLite (`linkedin_growth.db`) + helper. 4 tabelle: `existing_contacts`, `prospects`, `daily_quota`, `discovery_log`, `run_health`. Init idempotente.
- **`refresh_dedup.py`** — popola `existing_contacts` unificando Elenco Excel + `contact_status_check.json` + `connections_list.json`. Flag `--scrape` lancia il browser per refresh fresh della rete attuale.
- **`discovery_agent.py`** — scopre nuovi prospect via people-search (matrice ICP × paese × lingua × keyword) e via engagement scraping (commentatori di post recenti su hashtag target). Scoring 0-100, skip automatico di chi e' gia' in `existing_contacts`.
- **`sender_v3.py`** — invia richieste (senza nota) leggendo dalla queue `prospects`. Spread orario, pre-check stato profilo, detect weekly cap + CAPTCHA + commercial limit, auto-stop. Aggiorna DB.
- **`report_growth.py`** — dashboard CLI: progresso vs 5000, stato giornaliero, queue, ETA, top prospects. Export CSV opzionale.
- **`health_monitor.py`** — heartbeat verso healthchecks.io e/o alert Telegram. Subcommand `check-stale` lancia alert se `last_run.json` e' piu' vecchio di N ore.
- **`run_daily.ps1`** — orchestratore: init DB, discovery se queue < 100, sender 25/giorno 9-18, report, telegram summary.
- **`setup_scheduler.ps1`** — registra due Task Scheduler:
  - `LinkedInGrowthAgent_Daily` (ogni giorno 9:00, WakeToRun)
  - `LinkedInGrowthAgent_Healthcheck` (ogni 6h, alert se stale > 36h)

### Configurazione utente (decisioni gia' prese)
| Parametro | Valore |
|---|---|
| Account LinkedIn | Free |
| Volume giornaliero | 25 (con auto-stop su cap) |
| Fascia oraria invio | 9-18 (spread con jitter) |
| ICP segments | `cmo_head`, `ops_automation`, `founder_ai`, `consultant_agency` |
| Paesi | Italy + Germany + France + UK + Spain + Netherlands |
| Lingue | IT, EN (priorita' IT) |
| Note nella richiesta | Nessuna |
| Hashtag engagement | aimarketing, marketingautomation, martech, aitools, growthhacking, contentmarketing, seo, performancemarketing |
| Comportamento cap | Stop e riprendi dopo 7 giorni |
| Discovery target queue | 500 (initial bulk: 2000) |
| Esecuzione | Locale + Task Scheduler + Wake-on-Timer + alert cloud (healthchecks.io / Telegram) |

## File rilevanti

- `linkedin_growth.db` — DB SQLite (NON committare, NON cancellare).
- `last_run.json` — scritto da `run_daily.ps1` a fine run; letto da `health_monitor.py check-stale`.
- `growth_run.log` — log dell'orchestratore (rotation manuale).
- `growth_dashboard.csv` — export periodico dello stato (rigenerato da `report_growth.py --csv`).
- `Elenco linkedin (1).xlsx` — lista storica, usata solo per dedup (schema A-F, vedi sezione legacy).
- `contact_status_check.json`, `connections_list.json` — storici importati nel dedup pool.
- `.env` — credenziali LinkedIn + opzionali `HEALTHCHECK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Non committare MAI.

## Ambiente

- **Python**: `C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe` (v3.12, NON `py` di Microsoft Store).
- **Pacchetti**: `selenium`, `openpyxl`, `pandas`, `python-dotenv`. Standard library: `sqlite3`, `urllib.request`.
- **Chrome**: `C:\Program Files\Google\Chrome\Application\chrome.exe`. Profilo persistente in `%LOCALAPPDATA%\LinkedInAutomationProfile`.
- **PowerShell, NON bash**: `python -c "..."` non funziona; usare `& "<path>\python.exe" script.py` o file `.py`.

## Comandi

Sempre dalla cartella progetto.

```powershell
# === SETUP INIZIALE (una tantum) ===

# 1. Inizializza DB e importa contatti storici
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\refresh_dedup.py"

# 2. Refresh fresh della rete attuale (apre browser, 5-10 min)
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\refresh_dedup.py" --scrape

# 3. Bulk discovery iniziale (puo' richiedere 1-3h, browser aperto)
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\discovery_agent.py" --mode both --queue-target 2000 --max-queries 30 --max-hashtags 10

# 4. Registra Task Scheduler (PowerShell come Admin per WakeToRun)
.\setup_scheduler.ps1

# === USO QUOTIDIANO ===

# Esecuzione manuale completa (discovery se serve + sender + report)
.\run_daily.ps1

# Solo sender (se queue gia' piena)
.\run_daily.ps1 -SkipDiscovery

# Solo discovery (no invio)
.\run_daily.ps1 -SkipSender

# Test: dry-run del sender
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\sender_v3.py" --dry-run

# Test reale con limite 2 invii, ignora orario
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\sender_v3.py" --limit 2 --ignore-schedule

# === DASHBOARD ===

& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\report_growth.py" --week --csv

# === HEALTH MONITOR ===

# Stato configurazione monitor
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\health_monitor.py" status

# Test alert Telegram
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\health_monitor.py" telegram --message "test"

# Check stale manuale
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\health_monitor.py" check-stale --hours 36
```

### Setup opzionale: Telegram alerts

1. Aprire Telegram, chattare con `@BotFather`, comando `/newbot`, copiare il token.
2. Avviare una chat con il bot, mandare un messaggio.
3. Visitare `https://api.telegram.org/bot<TOKEN>/getUpdates`, copiare `chat.id`.
4. Aggiungere a `.env`:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

### Setup opzionale: Healthchecks.io (alert "non hai girato")

1. Account gratuito su https://healthchecks.io
2. Creare un check "LinkedIn growth", impostare "Period" a 24h, "Grace" a 6h.
3. Copiare l'URL ping (es. `https://hc-ping.com/<uuid>`).
4. Aggiungere a `.env`:
   ```
   HEALTHCHECK_URL=https://hc-ping.com/<uuid>
   ```
5. Configurare in healthchecks.io una integration (Telegram/email/Slack) per ricevere alert.

## Vincoli / gotchas

- **NON lanciare headless**: LinkedIn blocca i bot headless. Chrome sempre visibile.
- **NON aprire `linkedin_growth.db` in editor SQLite GUI durante l'esecuzione**: lock = errori scritture. (SQLite e' in WAL mode, lettura concorrente OK).
- **NON girare in cloud / VPS datacenter**: LinkedIn flagga IP datacenter, security challenge istantanea e poi ban account. L'esecuzione e' obbligatoriamente locale o su VPS con proxy residenziale italiano.
- **NON aprire l'xlsx in Excel durante una run che lo legge**: il salvataggio fallisce (rilevante solo per legacy `send_linkedin_requests.py`; il nuovo sistema usa DB).
- **Rate limit LinkedIn Free**: ~100-200 inviti/settimana (variabile, opaco). Il sender ha auto-detection del banner "weekly invitation limit": pausa 7gg automatica. Anche `discovery_agent.py` ha auto-stop per "commercial use limit".
- **Selettori DOM**: LinkedIn cambia le classi CSS spesso. Quando i risultati di ricerca tornano 0 ma lo screenshot li mostra, ispezionare con `inspect_selectors.py` (template in `%TEMP%\opencode\`). Funzioni candidate da aggiornare: `parse_search_card`, `collect_search_cards` in `discovery_agent.py`; `check_action_on_profile`, `click_connect_on_profile`, `click_send_in_modal` in `sender_v3.py`.
- **Modal "Invia senza nota" in Shadow DOM chiuso**: il confirmation modal (`<artdeco-modal>`) è renderizzato in un custom element con Shadow DOM chiuso, NON accessibile da `find_elements`, `outerHTML`, `page_source` o `querySelectorAll` nel main document. Anche gli iframe "pubblicità" e "preload" che LinkedIn mostra NON contengono il modal. Workaround in `click_send_in_modal`: dopo il connect click, provare prima i selettori classici (in caso LinkedIn torni al DOM normale), poi fare `body.click()` + `ActionChains.send_keys(Keys.ENTER)`. Il bottone "Invia senza nota" è il primary/default del modal, quindi ENTER lo attiva. Se in futuro LinkedIn cambia il default, questo fallback richiederà coordinate o `pyautogui`.
- **Check action scope a `<main>`**: per non confondere il bottone "Collegati" del profilo target con quello "+ Collegati" presente in ogni card della sidebar "Persone che potresti conoscere / Altri profili per te", tutte le detection (`check_action_on_profile`, `click_connect_on_profile`, `open_more_actions_menu`) sono scope-limited al `<main>` del profilo via `_profile_header_scope()`. Il check per "Collegati" nel sidebar altrimenti restituisce `action=connect` per un profilo che in realtà ha solo "Segui" nel proprio header.
- **Pending check allargato**: il bottone "In sospeso" può essere `<button>`, `<a>` o `[role='button']`; la detection in `check_action_on_profile` controlla text E aria-label. "Invia messaggio" NON è affidabile come segnale di connessione (appare anche per non-connessi su Free).
- **Login detection**: URL-based (`/feed`, `/mynetwork`, `/in/` => loggato; `/login`, `/signup`, `/checkpoint`, `/uas/` => non loggato). Non affidarsi a `Page.title`.
- **Task Scheduler "WakeToRun"**: per funzionare richiede `powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1` (vedi output di `setup_scheduler.ps1`).
- **DB SQLite location**: il path attuale del progetto e' su OneDrive. SQLite + OneDrive funzionano ma in rari casi OneDrive puo' lockare il file durante sync (errore "database is locked"). Se succede frequentemente, spostare `linkedin_growth.db` fuori da OneDrive (es. `%LOCALAPPDATA%\LinkedInGrowth\linkedin_growth.db`) e aggiornare `DB_PATH` in `growth_db.py`.

## Debug

- Log orchestratore: `growth_run.log`.
- Log scrape vecchio: `scrape_run.log`.
- Screenshot LinkedIn: `%TEMP%\linkedin_*.png`.
- Page source LinkedIn: `%TEMP%\linkedin_debug.html`.
- Query DB veloce:
  ```powershell
  & "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" -c "import sqlite3; c = sqlite3.connect('linkedin_growth.db'); [print(r) for r in c.execute(\"SELECT status, COUNT(*) FROM prospects GROUP BY status\")]"
  ```

## Non fare

- Non committare `.env`, `linkedin_growth.db`, `last_run.json`.
- Non aumentare il `--target` del sender oltre 30/giorno su account Free senza essere consapevoli del rischio ban.
- Non eliminare le tabelle del DB: per resettare la coda usare update SQL (`UPDATE prospects SET status='discovered' WHERE status='queued'`) invece di drop.
- Non far girare `discovery_agent.py` con `--max-queries` alti su Free: LinkedIn applica "commercial use limit" tipicamente dopo 5-10 query in pochi minuti, e ti blocca search per 30gg.
- Non sostituire il browser visibile con headless "per accelerare".
- Non rimuovere la spread temporale del sender per fare batch veloci.

## Rollback al sistema legacy

Lo script `send_linkedin_requests.py` (con `.bak` di backup) e' ancora funzionante per liste statiche. Per usarlo:

```powershell
# Sender legacy su Excel
& "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe" ".\send_linkedin_requests.py" --limit 2
```

Schema Excel `Elenco linkedin (1).xlsx`, foglio `Contatti`:
- A: `Nome e cognome`
- B: `Societa' di appartenenza`
- C: `Altre informazioni`
- D: `Contattare` (`y` = processa, vuoto/`n` = salta)
- E: `Inviato` (`y` = gia' inviato/connesso)
- F: `Inviato il` (timestamp)

Se la versione corrente fallisce:
```powershell
Copy-Item -LiteralPath ".\send_linkedin_requests.py.bak" -Destination ".\send_linkedin_requests.py" -Force
```
