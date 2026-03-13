# Memory Hub Gateway — Architektur, Einrichtung und Betrieb

## Übersicht

Das Gateway ermöglicht es ChatGPT (und anderen Remote-Clients), auf den lokalen **Memory Hub** zuzugreifen — einen SQLite-basierten, lokal laufenden Wissensspeicher für AI-Agents. Da der Memory Hub nur über `stdio` kommuniziert und bewusst kein Netzwerk öffnet, braucht es eine Mittlerschicht, die Remote-Calls sicher auf den lokalen Rechner bringt.

Die drei Hauptkomponenten:

```
┌───────────────────────────────────────────────────────────────────┐
│  Remote                                                           │
│                                                                   │
│   ChatGPT  ──── HTTPS ────►  Gateway  ──── lokal ────►  Companion│
│   (Tool Call)              (FastAPI,                (Python CLI,  │
│                             öffentlich)              läuft lokal) │
│                                  │                       │        │
│                                  │                       ▼        │
│                                  │              Memory Hub (stdio)│
│                                  │              SQLite + FTS5     │
└──────────────────────────────────┴───────────────────────────────-┘
```

**Gateway** — öffentlich erreichbarer HTTP-Server. Nimmt Tool-Calls von ChatGPT entgegen, erzwingt Idempotenz und Approval-Timeouts, leitet mutierende Calls zur Bestätigung an den Companion weiter.

**Companion** — lokaler Python-Prozess auf dem Mac. Authentifiziert sich am Gateway, zeigt Approval-Prompts im Browser an, führt freigegebene Tool-Calls gegen den Memory Hub aus und meldet Ergebnisse zurück.

**Memory Hub** — bleibt unverändert lokal. Läuft über `stdio`, kein Netzwerk, kein Cloud-Sync.

---

## Sicherheitsmodell

| Schicht | Mechanismus |
|---|---|
| Gerät ↔ Gateway | Refresh Token (UUID, SHA-256-gehashed in DB, im macOS Keychain lokal) |
| Companion ↔ Gateway | Kurzlebiges JWT (HS256, 15 min TTL), Bearer-Header |
| Mutierende Tool-Calls | Lokale Browser-Approval (127.0.0.1:47821) vor Ausführung |
| Replay-Schutz | Idempotency-Key + arguments_hash, Result-Cache 10 min |
| Timeout | 60 s Approval-Fenster, dann 408 statt hängender Verbindung |
| Allowlist | Deklarative YAML-Config, unbekannte Tools → 403 vor Routing |

---

## Tool-Tiers

Alle Memory-Hub-Tools sind in `allowlist.yaml` drei Tiers zugeordnet:

### TIER 1 — Auto-approved (read-only, kein Approval nötig)

Werden direkt am Gateway beantwortet (Endpunkt `/api/v1/direct-call`, noch nicht implementiert):

`search_memories`, `get_memory`, `list_recent_memories`, `get_project_context`,
`list_work_items`, `get_agent_context`, `list_memory_entities`, `list_memory_artifacts`,
`get_link_candidate`, `get_review_queue`, `list_handoffs`, `list_link_candidates`

### TIER 2 — Approval-gated (mutierend, Nutzerbestätigung erforderlich)

Laufen durch den vollständigen Approval-Lifecycle:

`create_memory`, `update_memory`, `archive_memory`, `create_work_item`,
`claim_work_item`, `complete_work_item`, `create_handoff`, `link_memories`,
`create_link_candidate`, `approve_link_candidate`, `reject_link_candidate`,
`mark_handoff_read`, `start_work_item_session`, `start_project_session`, `start_agent_session`

### TIER 3 — Excluded (nie exponiert)

`attach_artifact`, `attach_git_artifact`, `suggest_links`, `suggest_project_links`

→ Gateway gibt sofort **403** zurück, bevor der Call den Companion erreicht.

---

## Approval-Lifecycle

Jeder mutierende Tool-Call durchläuft eine Zustandsmaschine im Gateway:

```
                     ┌─────────┐
       ChatGPT ────► │ pending │
                     └────┬────┘
            60 s          │ Companion zeigt UI
            Timeout       │
               ▼          ▼
           expired    approved ──── Companion führt aus ────► executed
                       denied                                  failed
```

**Statusübergänge (erlaubt):**

```
pending  →  approved | denied | expired
approved →  executed | failed
```

Terminale Zustände (`denied`, `expired`, `executed`, `failed`) erlauben keine weiteren Übergänge.

**Idempotenz:**

- Jeder Call trägt einen `Idempotency-Key`-Header (wird vom Gateway generiert, falls nicht mitgegeben).
- Gleicher Key + gleicher Payload innerhalb von 10 min → gecachtes Ergebnis, kein neuer Approval-Prompt.
- Gleicher Key + anderer Payload → **409 Conflict**.

**HTTP-Statuscodes für ChatGPT:**

| Situation | Status |
|---|---|
| Call wurde ausgeführt | 200 mit Ergebnis |
| Nutzer hat abgelehnt | 403 `approval_denied` |
| Approval-Timeout (60 s) | 408 `approval_timeout` |
| Idempotency-Konflikt | 409 |
| Companion offline | 503 `local_companion_unavailable` *(wrk_beccab, pending)* |

---

## Einrichtung

### Voraussetzungen

- Python 3.12+
- macOS (Keychain-Backend für Refresh Token; Linux/Windows: noch nicht implementiert)
- Öffentlich erreichbarer Server für das Gateway (z. B. VPS, Railway, Fly.io)

### 1. Memory Hub einrichten

```bash
cd /Users/denniswillkomm/Documents/CODE/DataStore/memory-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
memory-hub init-db
```

### 2. Gateway einrichten

```bash
cd /Users/denniswillkomm/Documents/CODE/DataStore/gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Umgebungsvariablen setzen (z. B. in `.env` oder als Systemvariablen):

```bash
export GATEWAY_JWT_SECRET="<mindestens 32 zufällige Zeichen>"
export GATEWAY_DB_PATH="/pfad/zu/gateway.db"
# Optional:
export GATEWAY_ACCESS_TOKEN_TTL=900       # 15 min
export GATEWAY_REFRESH_TOKEN_TTL_DAYS=90
export GATEWAY_APPROVAL_TIMEOUT=60        # Sekunden
export GATEWAY_RESULT_TTL=600             # 10 min
export GATEWAY_HEARTBEAT_TIMEOUT=30       # Sekunden
```

Gateway starten:

```bash
gateway  # startet uvicorn auf 127.0.0.1:8080
```

Für den Produktivbetrieb hinter einem Reverse Proxy (nginx, Caddy) mit TLS.

### 3. Companion pairen

Einmaliger Pairing-Flow:

```bash
memory-hub companion pair --gateway https://deine-gateway-url.example.com --device-name "Dennis MacBook"
```

1. CLI öffnet den Browser mit einer Bestätigungsseite auf dem Gateway.
2. Nutzer klickt „Approve".
3. Gateway gibt einen Refresh Token aus.
4. CLI speichert den Token im macOS Keychain (nie in einer Textdatei).

Status prüfen:

```bash
memory-hub companion status
```

Token manuell erneuern:

```bash
memory-hub companion refresh --force
```

Gerät entpairen:

```bash
memory-hub companion unpair  # widerruft auch das Gerät am Gateway
```

### 4. ChatGPT verbinden

Im ChatGPT-GPT-Editor unter **Actions** eine neue Action anlegen:

- **Schema-URL:** `https://deine-gateway-url.example.com/openapi.json`
- **Authentifizierung:** keine (das Gateway authentifiziert intern via Companion-JWT)
- **Privacy Policy:** nach Bedarf

Relevante Tool-Call-Endpunkte, die ChatGPT nutzt:

```
POST /api/v1/tool-call          Mutierenden Tool-Call starten
GET  /api/v1/tool-call/{id}     Status pollen (async Alternative)
```

---

## API-Referenz

### Health

| Method | Path | Beschreibung |
|---|---|---|
| `GET` | `/health` | Liveness-Check → `{"ok": true}` |
| `GET` | `/health/companion` | Companion online/offline → `{"companion_online": bool, "last_seen": "..."}` |

### Pairing (Companion ↔ Gateway)

| Method | Path | Beschreibung |
|---|---|---|
| `POST` | `/api/v1/companion/pair/start` | Pairing-Request anlegen |
| `GET` | `/api/v1/companion/pair/poll/{request_id}` | Status pollen bis approved/denied/expired |
| `GET` | `/approve/device/{request_id}` | Bestätigungs-UI im Browser |
| `POST` | `/approve/device/{request_id}/action` | Approve / Deny (form POST) |
| `POST` | `/api/v1/companion/devices/{device_id}/revoke` | Gerät widerrufen |

### Token-Verwaltung (Companion ↔ Gateway)

| Method | Path | Beschreibung |
|---|---|---|
| `POST` | `/api/v1/companion/token/refresh` | Refresh Token → Access Token (JWT) + Rotation |

Request:
```json
{ "device_id": "uuid", "refresh_token": "uuid" }
```
Response:
```json
{
  "access_token": "eyJ...",
  "expires_at": "2026-03-13T12:15:00Z",
  "refresh_token": "neu-rotiert-uuid",
  "refresh_token_expires_at": "2026-06-11T12:00:00Z",
  "last_seen": "2026-03-13T12:00:00Z"
}
```

Fehler: `401` mit `{"error": "expired_refresh_token" | "device_revoked" | "invalid_refresh_token"}`

### Tool-Calls (ChatGPT → Gateway)

| Method | Path | Beschreibung |
|---|---|---|
| `POST` | `/api/v1/tool-call` | Tool-Call starten (long-poll bis Ergebnis oder 408) |
| `GET` | `/api/v1/tool-call/{request_id}` | Status pollen (async Alternative) |

Request:
```json
{
  "tool_name": "create_memory",
  "arguments": { "title": "...", "content": "..." }
}
```
Header: `Idempotency-Key: <uuid>` (optional, wird generiert falls nicht angegeben)

### Companion-seitige Approval-Endpunkte (Bearer JWT erforderlich)

| Method | Path | Beschreibung |
|---|---|---|
| `GET` | `/api/v1/companion/pending-requests` | Offene Approval-Requests abrufen |
| `POST` | `/api/v1/approval-requests/{id}/approve` | Request freigeben (pending → approved) |
| `POST` | `/api/v1/approval-requests/{id}/deny` | Request ablehnen (pending → denied) |
| `POST` | `/api/v1/approval-requests/{id}/confirm` | Ausführung melden (approved → executed/failed) |

---

## Typischer Ablauf: ChatGPT erstellt eine Memory

```
ChatGPT                  Gateway                  Companion (lokal)
   │                        │                           │
   │  POST /api/v1/tool-call│                           │
   │  tool_name=create_memory                           │
   │  Idempotency-Key: abc  │                           │
   ├───────────────────────►│                           │
   │                        │  DB: INSERT pending       │
   │                        │  state = pending          │
   │                        │                           │
   │    (long-poll offen)   │◄── GET pending-requests ──┤
   │                        │─── [{request_id, ...}] ──►│
   │                        │                           │ Browser öffnet:
   │                        │                           │ http://127.0.0.1:47821
   │                        │                           │ /approve/{request_id}
   │                        │                           │
   │                        │                           │ Nutzer klickt „Approve"
   │                        │◄── POST .../approve ──────┤
   │                        │  state = approved         │
   │                        │                           │ memory-hub create_memory(...)
   │                        │◄── POST .../confirm ──────┤
   │                        │  state=executed, result=…  │
   │                        │                           │
   │◄───────────────────────┤                           │
   │  200 { result: {...} } │                           │
```

Falls der Nutzer **nicht reagiert** (60 s Timeout):

```
Gateway ──► state = expired ──► 408 an ChatGPT
```

Falls der Nutzer **ablehnt**:

```
Companion ──► POST .../deny ──► state = denied ──► 403 an ChatGPT
```

---

## Offene Punkte (noch nicht implementiert)

| Was | Work Item |
|---|---|
| Companion-Heartbeat + 503 bei Offline | `wrk_beccab` (offen) |
| `/api/v1/direct-call` für TIER 1 Tools | kein Work Item |
| Linux `libsecret` / Windows DPAPI Keychain | kein Work Item |
| Gateway OAuth-Login + Device-Liste in UI | kein Work Item |
| Request-Queue im Companion (offline resilience) | kein Work Item |

---

## Konfigurationsreferenz

| Env-Variable | Standard | Beschreibung |
|---|---|---|
| `GATEWAY_JWT_SECRET` | `change-me-in-production` | HS256-Schlüssel, min. 32 Bytes |
| `GATEWAY_DB_PATH` | `data/gateway.db` | SQLite-Datenbankpfad |
| `GATEWAY_ACCESS_TOKEN_TTL` | `900` | Access-Token-Lebensdauer (Sekunden) |
| `GATEWAY_REFRESH_TOKEN_TTL_DAYS` | `90` | Refresh-Token-Lebensdauer (Tage) |
| `GATEWAY_APPROVAL_TIMEOUT` | `60` | Max. Wartezeit auf Nutzer-Approval (Sekunden) |
| `GATEWAY_RESULT_TTL` | `600` | Idempotenz-Cache-Fenster (Sekunden) |
| `GATEWAY_HEARTBEAT_TIMEOUT` | `30` | Companion gilt als offline nach X Sekunden ohne Heartbeat |
