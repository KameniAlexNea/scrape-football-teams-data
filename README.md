# scrape-football-teams-data

Agentic football data scraper. You give a **league name** and a **link**; a Claude
agent drives a real Playwright browser to explore that site — like a human — and
extract, per season over the last ~10 years, each club's **squad** (players,
positions, ages, nationalities, values), **manager**, **final league-table
position**, and **match results with scores**. Data is saved incrementally into
one JSON file per league, so interrupted runs keep their progress.

The agent does **not** know the site in advance. It has no hardcoded URLs,
site-specific recipes, or data normalisation rules: it reads each page, discovers
the structure (tabs, links, season selectors), and writes values exactly as the
site shows them. This works on FotMob, league official sites, and anywhere else.

## How it works

```
footy-scrape <league-link> --league "Premier League"
   │
   ├─ BrowserSession (Playwright Chromium, headless)
   ├─ ToolExecutor  ── navigate / page_snapshot / click / click_text /
   │                  select_option / fill / scroll / wait / screenshot /
   │                  save_standing / save_squad / save_match
   ├─ ScrapeAgent  ── claude-agent-sdk: Claude Code subprocess calling our tools
   │                  through an in-process MCP server; streams every step back
   ├─ prompts      ── discovery-first: read the page, act like a human, save what you see
   └─ LeagueStore  ── validating JSON repo; atomic incremental merges
                      data/<league>.json
```

## Setup

Requirements: Python 3.11+, `uv`, Playwright Chromium, and the **`claude` CLI**
(Claude Code) — the agent runs on `claude-agent-sdk`, which spawns `claude` as
a subprocess and calls our browser tools through an in-process MCP server.

```bash
# 1. Install dependencies + package (uses uv)
uv sync

# 2. Install the Playwright browser
uv run footy-scrape --install-browsers

# 3. Make sure the `claude` CLI is installed and logged in:
#      npm install -g @anthropic-ai/claude-code   # or your preferred install
#      claude                                    # one-time login
#    OR point the SDK at any Anthropic-compatible endpoint via .env:
cp .env.example .env   # set ANTHROPIC_API_KEY (+ ANTHROPIC_BASE_URL for a gateway/self-hosted server)
```

## Usage

```bash
# You provide the league name and a link — the agent figures out the site.
uv run footy-scrape "https://www.fotmob.com/leagues/47/overview/premier-league" --league "Premier League"
uv run footy-scrape "https://www.premierleague.com/en/clubs" --league "Premier League"
uv run footy-scrape "https://www.laliga.com/en-GB/laliga-easports/clubs" --league "La Liga"

# Customise
uv run footy-scrape "https://www.fotmob.com/leagues/47/overview/premier-league" \
  --league "Premier League" \
  --seasons "2024-25,2023-24,2022-23" \
  --headful \
  --max-steps 150

# Default behaviour: last 10 seasons, output data/<league>.json
# Season shorthand: --seasons "last:5"
```

### Watching the agent work
The browser runs headless by default. To **see what the agent is doing live**, open a real
browser window and slow it down:

```bash
uv run footy-scrape "https://www.fotmob.com/leagues/47/overview/premier-league" \
  --league "Premier League" --headful --slow-mo 300
```

- `--headful` opens a visible Chromium window — you watch every page load, click, scroll and save.
- `--slow-mo <ms>` inserts a pause between browser actions (default 0) so you can follow along.
- Set `FOOTY_HEADLESS=false` in `.env` to default to a visible window without the flag.
- The agent can also save screenshots to `data/<league>/screenshots/` via its `screenshot` tool.
- On a machine with no display (e.g. a server), wrap headful runs with `xvfb-run`.

### Reading the logs — three levels of detail
The agent streams its execution with loguru. Pick the detail level you want:

| Flag | Level | What you see |
| --- | --- | --- |
| *(none)* | INFO | Concise per-step progress + exactly what was saved, e.g. `� saved: 2024-25 standings (20 rows; +20 added)`, `👥 saved: 2024-25 squad for Arsenal (31 players total)`, or `already saved: 2024-25 matches (380 rows)` |
| `--verbose` | DEBUG | Every tool call with its **full params** (JSON), plus a result summary per step |
| `--trace` | TRACE | **Full-fidelity**: complete tool-call params and the **full raw tool result** for every iteration (page snapshots, saved payloads, …) |

```bash
uv run footy-scrape "https://www.fotmob.com/leagues/47/overview/premier-league" \
  --league "Premier League" --trace
```

The agent prints progress and saves as it goes; re-running the same league
**merges** into the existing file rather than overwriting it.

### How the agent behaves
- It knows nothing about the site: it calls `page_snapshot`, reads the visible text and links, and
  decides what to click next — no preset URLs or steps.
- It looks for relevant tabs/links ("Squad", "Table", "Standings", "Results", ...) in whatever
  wording the site uses, handles season selectors, waits for lazy content, and dismisses cookie banners.
- Positions, club names and player names are recorded **exactly as the site writes them** — no
  translation or normalisation.
- Progress is streamed live with **loguru** (timestamped, coloured): each agent step is logged as it
  happens (`[step 3] Opening page — url=…`, `Saving data — season=2024-25, club=Arsenal`, …), plus a
  final summary with step/tool counts. Use `--verbose` for debug-level detail (page reads, tool results).

## Output schema (per league JSON)

```jsonc
{
  "league": "Premier League",
  "url": "https://www.fotmob.com/leagues/47/overview/premier-league",
  "scraped_at": "2026-08-30T12:00:00Z",
  "seasons": {
    "2024-25": {
      "standings": [
        { "club": "Arsenal", "position": 2, "played": 38, "won": 24,
          "drawn": 6, "lost": 8, "goals_for": 79, "goals_against": 36,
          "goal_difference": 43, "points": 78 }
      ],
      "matches": [
        { "date": "2024-08-17", "home_team": "Arsenal", "away_team": "Wolves",
          "home_score": 2, "away_score": 0 }
      ],
      "clubs": {
        "Arsenal": {
          "final_position": 2,
          "manager": { "name": "Mikel Arteta", "nationality": "Spain" },
          "squad": [
            { "name": "Bukayo Saka", "shirt_number": 7, "position": "Right Winger",
              "age": 24, "nationality": "England",
              "height_cm": 178, "market_value": "€94.3M" }
          ],
          "sources": ["https://www.fotmob.com/teams/9825/squad/arsenal"]
        }
      }
    }
  }
}
```

Site-specific fields are kept in each object's `extra` map, so nothing is lost.

## Commands

| Command | Purpose |
| --- | --- |
| `footy-scrape --install-browsers` | Install Playwright Chromium |
| `footy-scrape <url>` | Run the agent (see options with `--help`) |
| `footy-scrape --help` | Show all options |

## Configuration (env / `.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (optional if `claude` CLI is logged in) |
| `ANTHROPIC_BASE_URL` | — | Anthropic-compatible endpoint (gateway / self-hosted / any compatible server) |
| `ANTHROPIC_MODEL` | — | Model override (wins over `FOOTY_MODEL`) |
| `FOOTY_MODEL` | `claude-sonnet-4-5` | Claude model for the agent |
| `FOOTY_HEADLESS` | `true` | Run the browser headless |
| `FOOTY_OUTPUT_DIR` | `data` | Where league JSON + screenshots go |
| `FOOTY_TIMEOUT_MS` | `30000` | Browser navigation timeout |
| `FOOTY_SNAPSHOT_MAX_CHARS` | `30000` | Max page text per snapshot |

## Tests

```bash
uv run pytest
```

## Notes / best effort

- Not every site exposes 10 years of history, season selectors, or full tables.
  The agent records what it can and reports gaps in its final message.
- Sites with aggressive bot protection may need a headed run (`--headful`) or a
  real user agent; that is a per-site tuning concern.

