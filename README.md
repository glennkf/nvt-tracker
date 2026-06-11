# NVT Role Tracker

Role tracking dashboard for **Network Voice Toastmasters** — hosted on GitHub Pages.

## What it tracks
- Role fills per member (count + which roles)
- Distinct roles performed
- Meeting attendance vs. participation
- Pathways Level 1–5 role requirement checklist

## Repo structure
```
index.html              ← Web app (open this in a browser)
data/
  data.json             ← All tracker data lives here
scripts/
  update_tracker.py     ← Run this after each meeting
```

## Setup (one time)

### 1. Create the GitHub repo
1. Go to github.com → New repository → name it `nvt-tracker` (or similar)
2. Push this folder to the repo
3. Go to Settings → Pages → Source: Deploy from branch → branch: `main`, folder: `/root`
4. Your site will be live at `https://yourusername.github.io/nvt-tracker/`

### 2. Install Python dependency
```bash
pip install pdfplumber
```

### 3. Backfill historical meetings
Save your past FTH printed agenda PDFs to a folder, then:
```bash
python scripts/update_tracker.py path/to/pdfs/folder/
```

## After each meeting (2–3 min workflow)

1. In FTH, click **Print Agenda** and save the PDF
2. Run the parser:
   ```bash
   python scripts/update_tracker.py ~/Downloads/Printed_Agenda_June_17_2026.pdf
   ```
3. Review the parsed output shown in your terminal
4. Confirm `y` to add the meeting
5. Enter any manual roles (Table Topics Speakers, Mentors, etc.) when prompted
6. Push to GitHub:
   ```bash
   git add data/data.json && git commit -m "Add June 17 meeting" && git push
   ```
7. Site updates automatically in ~1 minute

## Manual role entry (data.json)

For roles not in FTH (Introductory Mentor, Club Mentor, Specialized Roles), add to the member's `manualRoles` array in `data.json`:

```json
{
  "name": "Glenn Fernandes",
  "active": true,
  "manualRoles": [
    { "role": "Introductory Mentor", "date": "2026-04-15", "meeting": "April 15, 2026" },
    { "role": "Table Topics Speaker", "date": "2026-05-06", "meeting": "May 6, 2026" }
  ]
}
```

## Adding/removing members

Edit the `members` array in `data.json`. Set `"active": false` to hide a member without deleting their history.

## Role aliases

The `roleAliases` section in `data.json` maps raw FTH role names to canonical names. Add entries here if your club adds custom roles. Set the value to `null` to ignore a role entirely.

## Updating Pathways level requirements

Level requirements are in `data.json` under `levelRequirements`. Edit there if TM International changes the requirements.
