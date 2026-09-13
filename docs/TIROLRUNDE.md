# Tirolrunde — Multi-Map Session (Vision / spätere Extension)

**Status:** Idee festgehalten, **noch nicht implementiert**.  
**Zweck dieses Docs:** Damit ein späterer Agent (oder Mensch) weiß, was mit „Tirolrunde“, Portal-Toren und Session-Files gemeint ist — ohne die Terrain-Pipeline damit zu vermischen.

Verwandt: [KONZEPT.md](KONZEPT.md) (Geodaten → Level), [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md) (einzelnes Level).

---

## Produktziel

Spieler fährt die schönsten Kurven eines Passes (z. B. Fernpass); die Zeit wird aufgezeichnet. Am Kartenrand wählt er bewusst weiter (z. B. Richtung Timmelsjoch oder Brenner). Ein **Hard-Switch** lädt die nächste BeamNG-Map. Session bleibt erhalten:

- bisherige **Segmentzeiten** + laufende Gesamtzeit
- **Einstellungen** (Tageszeit, Wetter, Verkehr, Umwelt)
- am Ende der „Tirol-Runde“: **Übersicht aller Zeiten**

Beliebig viele selbst gebaute Karten sollen sich so zu einem Graphen verknüpfen lassen — ohne Engine-World-Streaming.

---

## Was BeamNG kann / nicht kann

| Realität | Konsequenz |
|----------|------------|
| Kein Seamless-Streaming mehrerer Levels | Eine Map = ein Load; Übergang = bewusster Reload |
| `core_levels.startLevel("/levels/<name>/main.level.json")` | Hard-Switch per Trigger/Lua machbar |
| Stock-Race-/Freeroam-Zeiten überleben Map-Wechsel nicht | Eigene Zeitnahme nötig |
| Settings/Wetter/Fahrzeuge bleiben nicht „magisch“ | Explizit speichern und nach Load wiederherstellen |
| Globale GE-Extensions mit `setExtensionUnloadMode(..., "manual")` können über Level-Wechsel leben | Session-Kern dort (oder Dateien + Reload) |

Saubere Übergangsstellen (Tunnel, Galerie, Ortsschild, beschriftetes Tor am Rand) machen den Hard-Switch akzeptabel.

---

## Architektur-Idee (für spätere Implementierung)

### Kern: Extension „Tirolrunde“

Map-unabhängige **GE-Lua-Extension** (nicht `mainLevel.lua` einer einzelnen Karte — die wird beim Unload verworfen):

1. Session starten / Segmentzeiten führen (während Load **pausieren**)
2. Vor Level-Wechsel State schreiben
3. Zielkarte laden
4. Nach `onWorldReadyState == 2`: Settings anwenden, Spawn setzen, Timer fortsetzen, UI aktualisieren
5. Runde beenden → Summary aller Segmente

### Persistenz: Dateien neben den Levels

Neben (oder unter) den Levels abgelegte JSON-Dateien halten die Welt konsistent über Reloads — die Extension liest/schreibt sie.

Vorschlag (Schema noch frei, Namen nur Orientierung):

```text
# Session (kurzlebig, aktive Fahrt)
tirolrunde/session.json
  - session_id, started_at
  - settings: { time_of_day, weather, traffic, environment, ... }
  - segments: [ { id, map, started_at, finished_at, duration_s }, ... ]
  - active_segment, pending_portal

# Portal-Graph (statisch, pro Tour / Content-Pack)
tirolrunde/portals.json   # oder portals/*.json pro Map
  - gate_id → { from_level, to_level, spawn_name_or_xyz, segment_id, label }

# Optional: Bestzeiten / abgeschlossene Runden
tirolrunde/history.json
```

Prinzip: **Karten bauen = Terrain/Content; Verknüpfen = Portale beschriften.** Neue Karte = neuer Level-Ordner + Portal-Einträge, ohne die Extension neu zu erfinden.

### In-Map: beschriftete Tore nahe dem Rand

- Sichtbares Tor / Trigger nahe Kartenrand (oder Tunnelausfahrt)
- Label für den Spieler („Richtung Timmelsjoch“, „Richtung Brenner“)
- `gate_id` verknüpft mit `portals.json`
- Zielkarte definiert passenden **Spawn** hinter dem korrespondierenden Eingangstor

Spielerfluss:

```text
Fernpass (Segment A, Timer läuft)
  → Wahl am Tor
  → session.json schreiben (Zeiten + Settings)
  → startLevel(Timmelsjoch | Brenner)
  → Settings + Spawn wiederherstellen
  → Segment B, Timer weiter
  → … beliebig weiter verzweigen …
  → „Runde beenden“ → alle Zeiten sichtbar
```

---

## Abgrenzung zur Autoroad-Pipeline

- **Jetzt (dieses Repo):** einzelne spielbare Pass-Levels aus OGD (Heightmap, Masken, Galerien, …).
- **Später (Tirolrunde):** Gameplay-/Session-Schicht *über* mehreren fertigen Levels.
- Keine Abhängigkeit der Build-Pipeline von Tirolrunde-Files; optional später Generator-Hinweise für Portal-Spawns am BBOX-Rand.

---

## Nicht-Ziele (vorerst)

- Echtes Streaming / eine einzige Mega-Heightmap aus allen Pässen
- Fortsetzung der offiziellen BeamNG-Race-UI über Maps hinweg
- Online-Leaderboards (lokal reicht zuerst)

---

## Wenn du das baust — Checkliste für Agents

1. GE-Extension + `modScript.lua` mit manual unload; Hooks: Trigger/Portal, `onClientEndMission`, `onWorldReadyState`
2. JSON-Schema für Session + Portale festnageln; Load-Zeit aus der Wertung nehmen (pausieren)
3. Settings-Restore nach Load verifizieren (Tageszeit/Wetter/Verkehr/Umwelt — je nach BeamNG-API der Zielversion)
4. Pro Testkarte: ein Tor raus, ein Tor rein, Roundtrip A→B→A
5. Summary-UI oder zumindest Console/Imgui-Liste der Segmentzeiten
6. Content-Pack-Doku: „so beschriftest du ein Tor / so trägst du ein Portal ein“

Referenz-Threads/Konzepte aus der Ideenfindung: Level-Wechsel via `core_levels.startLevel`; Persistenz per GE-State und/oder `jsonWriteFile`/`jsonReadFile` (nicht `settings` als Dumping-Ground).
