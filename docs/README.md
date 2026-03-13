# Memory Hub Gateway — Dokumentation

## Was ist das?

Das Memory Hub Gateway ermöglicht es externen KI-Assistenten (ChatGPT, Claude web) auf deinen persönlichen **Memory Hub** zuzugreifen — ohne dass deine Daten jemals deinen Mac verlassen.

---

## Architektur

```
ChatGPT / Claude web
        │
        │  HTTPS (OpenAPI Action / MCP-over-SSE)
        ▼
┌───────────────────────┐
│   Railway Gateway     │  ← läuft in der Cloud (railway.app)
│   (FastAPI + SQLite)  │  ← Warteschlange für Tool-Requests
└───────────┬───────────┘
            │  Heartbeat + Tool-Ergebnisse (HTTPS)
            ▼
┌───────────────────────┐
│   Companion           │  ← läuft auf deinem Mac (Terminal)
│   (Python Prozess)    │  ← pollt Gateway, führt Tools aus
└───────────┬───────────┘
            │  MCP stdio (lokal)
            ▼
┌───────────────────────┐
│   Memory Hub          │  ← läuft lokal, Daten bleiben hier
│   (SQLite + FTS)      │
└───────────────────────┘
```

### Wie ein Tool-Call abläuft

1. ChatGPT ruft z.B. `searchMemories` auf → POST an Railway Gateway
2. Gateway legt Request in die Queue (SQLite, `state=pending`)
3. Companion auf deinem Mac sendet alle 2s einen Heartbeat → sieht den pending Request
4. **Tier 1** (read-only): Companion führt Tool automatisch aus
5. **Tier 2** (write): Browser öffnet sich auf deinem Mac → du bestätigst
6. Companion führt Tool lokal via MCP stdio aus → schickt Ergebnis an Gateway
7. Gateway liefert Ergebnis an ChatGPT zurück

### Warum so komplex?

**Sicherheit.** Der Gateway in der Cloud hat keinen Zugriff auf deine Daten — er ist nur ein Message-Broker. Memory Hub und alle deine Daten bleiben auf deinem Mac.

---

## Komponenten

| Komponente | Wo | Repo |
|---|---|---|
| Gateway (FastAPI) | Railway (Cloud) | `DataStore/gateway` |
| Companion (Python) | Dein Mac (Terminal) | `DataStore/memory-hub` |
| Memory Hub (MCP Server) | Dein Mac (lokal) | `DataStore/memory-hub` |
| ChatGPT Action | chatgpt.com | OpenAPI Spec |

---

## Tool-Tiers

### Tier 1 — Automatisch (kein Browser-Dialog)
- `search_memories` — Volltext-Suche
- `get_memory` — Memory by ID abrufen
- `list_recent_memories` — Letzte Änderungen
- `get_project_context` — Projekt-Kontext
- `list_work_items` — Work Items auflisten

### Tier 2 — Braucht deine Bestätigung (Browser öffnet sich)
- `create_memory` — Memory anlegen
- `update_memory` — Memory aktualisieren
- `archive_memory` — Memory archivieren
- `create_work_item` — Work Item anlegen

---

## Bedienungsanleitung

### Voraussetzungen

```bash
cd /Users/denniswillkomm/Documents/CODE/DataStore/memory-hub
```

### 1. Pairen (einmalig, oder nach Railway-Deploy)

Das Pairing verbindet deinen Mac mit dem Gateway. Es muss wiederholt werden wenn Railway neu deployed wurde (Datenbank wird zurückgesetzt).

```bash
PYTHONPATH=src .venv/bin/memory-hub companion pair --gateway https://web-production-6a48a.up.railway.app
```

→ Browser öffnet sich automatisch → **„Approve"** klicken → warten bis Terminal `"paired": true` zeigt

### 2. Companion starten

```bash
PYTHONPATH=src .venv/bin/memory-hub companion run
```

→ Läuft im Vordergrund, **Terminal offen lassen**
→ Ausgabe: `Starting companion event loop for gateway: https://...`
→ Danach stille Hintergrundarbeit — Logs erscheinen wenn ChatGPT etwas aufruft

**Stoppen:** `Ctrl+C`

### 3. Status prüfen

```bash
PYTHONPATH=src .venv/bin/memory-hub companion status
```

### 4. ChatGPT Action aktualisieren

OpenAPI-Spec liegt unter:
```
/Users/denniswillkomm/Documents/CODE/DataStore/gateway/openapi-chatgpt.json
```

In ChatGPT: **Explore GPTs → Deinen GPT bearbeiten → Actions → Schema ersetzen**

---

## Verfügbare ChatGPT-Operationen

| Operation | Beschreibung | Tier |
|---|---|---|
| `searchMemories` | Volltext-Suche | auto |
| `getMemory` | Memory per ID abrufen | auto |
| `listRecentMemories` | Letzte Memories | auto |
| `getProjectContext` | Projekt-Kontext | auto |
| `listWorkItems` | Work Items | auto |
| `createMemory` | Memory anlegen | approval |
| `updateMemory` | Memory aktualisieren | approval |
| `archiveMemory` | Memory archivieren | approval |
| `createWorkItem` | Work Item anlegen | approval |

---

## Wichtige URLs

| Was | URL |
|---|---|
| Gateway (Railway) | https://web-production-6a48a.up.railway.app |
| Health Check | https://web-production-6a48a.up.railway.app/health |
| GitHub Repo | https://github.com/denniswillkomm-del/memory-hub-gateway |

---

## Bekannte Einschränkungen

### Pairing nach jedem Deploy
Railway verwendet eine ephemere SQLite-Datenbank — bei jedem Deploy wird sie zurückgesetzt. Workaround: nach jedem Deploy neu pairen (Schritt 1).

**Lösung:** Railway Volume (Pro Plan) oder PostgreSQL statt SQLite.

### Companion manuell starten
Der Companion muss manuell im Terminal gestartet werden und stirbt wenn der Mac in den Ruhemodus geht (nach Aufwachen ggf. neu starten).

**Lösung:** macOS LaunchAgent einrichten (noch offen).

### Volltextsuche mit Bindestrichen
Suchbegriffe mit Bindestrichen (z.B. `claude-code`) schlagen fehl — der Bindestrich wird als FTS-Minus-Operator interpretiert.

**Status:** Work Item `wrk_a557c3e092fe4034af6af4d1c2ef318b` offen.

---

## Offene nächste Schritte

1. **Persistente DB** — Railway Volume oder PostgreSQL damit Pairing überlebt
2. **macOS LaunchAgent** — Companion automatisch im Hintergrund
3. **MCP-over-SSE** — Claude web Integration (Work Item `wrk_f1e46bf0719c48b197361d92f54b0897`)
4. **FTS Bindestrich-Bug** — Work Item `wrk_a557c3e092fe4034af6af4d1c2ef318b`

---

## Technologie-Stack

| Schicht | Technologie |
|---|---|
| Gateway API | Python 3.13, FastAPI, uvicorn |
| Datenbank | SQLite (WAL-Modus) |
| Authentifizierung | JWT (HS256), Refresh Token Rotation |
| Deployment | Railway, Docker |
| Companion | Python asyncio, urllib |
| Memory Hub | SQLite FTS5, MCP stdio |
| ChatGPT Integration | OpenAPI 3.1.0 Actions |
