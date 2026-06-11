#!/usr/bin/env python3
"""
NVT Role Tracker - PDF Parser & data.json Updater
Usage: python scripts/update_tracker.py path/to/agenda.pdf [--auto]
       python scripts/update_tracker.py path/to/folder/ [--auto]

Requires: pip install pdfplumber
"""
import json, re, sys, argparse
from pathlib import Path
from collections import defaultdict

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber")
    sys.exit(1)

DATA_PATH = Path(__file__).parent.parent / "data" / "data.json"

CREDENTIAL_PATTERN = re.compile(
    r',\s*(DTM|ACB|ACS|ACG|ALB|ALS|CC|CL|CTM|TM|'
    r'Pathways[\s\w]+\d*|MS\d+|VC\d+|LD\d+|SR\d+)[^,]*',
    re.IGNORECASE
)

ROLE_ALIASES = {
    "grammarian/word of the day": "Grammarian",
    "grammarian/word of the day report": "Grammarian",
    "ah-counter": "Grammarian", "ah counter": "Grammarian",
    "evaluator #1": "Evaluator", "evaluator #2": "Evaluator",
    "evaluator #3": "Evaluator", "evaluator #4": "Evaluator",
    "speaker #1": "Speaker", "speaker #2": "Speaker",
    "speaker #3": "Speaker", "speaker #4": "Speaker",
    "presidents welcome": "President's Welcome",
    "presidents closing remarks": "President's Welcome",
    "secretary/role call": "Secretary",
    "sergeant at arms": "Sergeant at Arms",
    "toastmaster return from break": None,
    "timer report": None, "voting results": None,
    "best speaker/evaluator": None, "break": None,
}

SKIP_STARTS = (
    "path / project", "project title", "opens meeting",
    "evaluates the meeting", "impromptu speaking", "https://", "meeting notes"
)

# These look like names but are credential fragments
CREDENTIAL_FRAGMENTS = re.compile(
    r'^(DTM|ACB|ACS|ACG|MS\d+|VC\d+|LD\d+|SR\d+|Pathways\s+\w+|Pathways\s+SR\d+|'
    r'Pathways\s+Mentor|ACL|CC|CL|TM|ACG|ALB|ALS)$', re.IGNORECASE
)

months = {'january':'01','february':'02','march':'03','april':'04','may':'05',
          'june':'06','july':'07','august':'08','september':'09',
          'october':'10','november':'11','december':'12'}

TIME_RE = re.compile(r'^(\d{1,2}:\d{2}(?:AM|PM))', re.IGNORECASE)
TIME_MAX_X = 80
MEMBER_MIN_X = 350
ROLE_MIN_X = 80
ROLE_MAX_X = 350

def clean_name(raw):
    if not raw or raw.strip() in ('-', '', '—'): return None
    cleaned = CREDENTIAL_PATTERN.sub('', raw)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().strip(',').strip()
    if not cleaned or len(cleaned) < 3: return None
    if CREDENTIAL_FRAGMENTS.match(cleaned): return None
    return cleaned

def normalize_role(r):
    if not r: return None
    role_text = r.split('\n')[0].strip()
    low = role_text.lower()
    for k, v in ROLE_ALIASES.items():
        if k == low: return v
    if any(low.startswith(s) for s in SKIP_STARTS): return None
    return role_text

def parse_pdf(path):
    roles_by_time = {}
    attending = []
    not_attending = []
    title = None
    date = None

    with pdfplumber.open(path) as pdf:
        full_text = ""
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 3: continue
                    t_cell = str(row[0] or '').strip()
                    if not TIME_RE.match(t_cell): continue
                    time_key = TIME_RE.match(t_cell).group(1)
                    norm = normalize_role(str(row[1] or ''))
                    if norm is None: continue
                    member = clean_name(str(row[2] or ''))
                    if member:
                        roles_by_time[time_key] = {'role': norm, 'member': member}

            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            rows_by_y = defaultdict(list)
            for w in words:
                y_bucket = round(w['top'] / 6) * 6
                rows_by_y[y_bucket].append(w)
            sorted_ys = sorted(rows_by_y.keys())

            for idx, y in enumerate(sorted_ys):
                row_words = rows_by_y[y]
                time_words = [w for w in row_words if w['x0'] < TIME_MAX_X]
                if not time_words: continue
                time_text = ' '.join(w['text'] for w in sorted(time_words, key=lambda w: w['x0']))
                m = TIME_RE.match(time_text)
                if not m: continue
                time_key = m.group(1)
                if time_key in roles_by_time: continue

                role_words = [w for w in row_words if ROLE_MIN_X <= w['x0'] < ROLE_MAX_X]
                member_words = [w for w in row_words if w['x0'] >= MEMBER_MIN_X]

                if not role_words and idx > 0:
                    prev_row = rows_by_y[sorted_ys[idx - 1]]
                    if not [w for w in prev_row if w['x0'] < TIME_MAX_X]:
                        role_words = [w for w in prev_row if ROLE_MIN_X <= w['x0'] < ROLE_MAX_X]

                if not role_words and idx + 1 < len(sorted_ys):
                    next_row = rows_by_y[sorted_ys[idx + 1]]
                    if not [w for w in next_row if w['x0'] < TIME_MAX_X]:
                        role_words = [w for w in next_row if ROLE_MIN_X <= w['x0'] < ROLE_MAX_X]
                        if not member_words:
                            member_words = [w for w in next_row if w['x0'] >= MEMBER_MIN_X]

                role_text = ' '.join(w['text'] for w in sorted(role_words, key=lambda w: w['x0']))
                member_text = ' '.join(w['text'] for w in sorted(member_words, key=lambda w: w['x0']))
                norm = normalize_role(role_text)
                if norm is None: continue
                member = clean_name(member_text)
                if member:
                    roles_by_time[time_key] = {'role': norm, 'member': member}

            full_text += (page.extract_text() or "") + "\n"

    tm = re.search(r'Agenda Item for (.+)', full_text)
    if tm:
        raw_title = tm.group(1).strip().split('\n')[0]
        title = re.sub(r'\s+Member\s*$', '', raw_title).strip()
        dm = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})', title, re.IGNORECASE)
        if dm:
            date = f"{dm.group(3)}-{months[dm.group(1).lower()]}-{dm.group(2).zfill(2)}"

    a_block = re.search(r'(?<!\bNot )Attending:\s*(.+?)(?=Not Attending:|Meeting Notes:|https://)', full_text, re.DOTALL)
    if a_block:
        for name in a_block.group(1).replace('\n', ',').split(','):
            c = clean_name(name.strip())
            if c and len(c.split()) >= 2 and not CREDENTIAL_FRAGMENTS.match(c):
                attending.append(c)

    na_block = re.search(r'Not Attending:\s*(.+?)(?=Meeting Notes:|https://|$)', full_text, re.DOTALL)
    if na_block:
        for name in na_block.group(1).replace('\n', ',').split(','):
            c = clean_name(name.strip())
            if c and len(c.split()) >= 2 and not CREDENTIAL_FRAGMENTS.match(c):
                not_attending.append(c)

    return {
        'title': title, 'date': date,
        'roles': list(roles_by_time.values()),
        'attending': list(dict.fromkeys(attending)),
        'not_attending': list(dict.fromkeys(not_attending))
    }

def load_data():
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def find_member(name, members):
    if not name: return None
    lower = name.lower()
    for m in members:
        if m['name'].lower() == lower: return m['name']
    parts = name.split()
    for m in members:
        mparts = m['name'].split()
        if (len(parts) >= 2 and len(mparts) >= 2 and
            mparts[-1].lower() == parts[-1].lower() and
            mparts[0][0].lower() == parts[0][0].lower()):
            return m['name']
    for m in members:
        if name.lower() in m['name'].lower() or m['name'].lower() in name.lower():
            return m['name']
    return None

def preview_parsed(parsed, data):
    members = data['members']
    print(f"\n{'='*60}")
    print(f"  PARSED: {parsed['title']}")
    print(f"  Date:   {parsed['date'] or 'NOT DETECTED'}")
    print(f"{'='*60}")

    unmatched = []
    print(f"\n  Roles found ({len(parsed['roles'])}):")
    for r in parsed['roles']:
        matched = find_member(r['member'], members)
        status = f"  -> matched: {matched}" if matched else "  -> !! NO MATCH"
        if not matched: unmatched.append(r['member'])
        print(f"    {r['role']:<30} {r['member']:<35}{status}")

    print(f"\n  Attending ({len(parsed['attending'])}):")
    for n in parsed['attending']:
        matched = find_member(n, members)
        print(f"    {n:<35} {'-> ' + matched if matched else '!! no match'}")

    print(f"\n  Not attending: {', '.join(parsed['not_attending'])}")

    if unmatched:
        print(f"\n  !! WARNING: {len(unmatched)} role(s) could not be matched to members:")
        for n in unmatched:
            print(f"    - '{n}'  (check spelling or add to data.json members list)")

    return unmatched

def prompt_manual_roles(data):
    print(f"\n{'-'*60}")
    print("  MANUAL ROLE ENTRY")
    print("  Add Table Topics Speakers, Mentors, Specialized Roles etc.")
    print("  Format: Name | Role   (press Enter with no input to finish)")
    print(f"{'-'*60}")
    manual = []
    while True:
        entry = input("  > ").strip()
        if not entry: break
        if '|' not in entry:
            print("  Format must be: Name | Role"); continue
        name, role = [p.strip() for p in entry.split('|', 1)]
        matched = find_member(name, data['members'])
        if not matched:
            print(f"  '{name}' not found in members list. Skipping.")
        else:
            manual.append({'member': matched, 'role': role})
            print(f"  Added: {matched} -> {role}")
    return manual

def process_pdf(pdf_path, auto=False):
    print(f"\nProcessing: {pdf_path}")
    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found.")
        sys.exit(1)

    data = load_data()
    parsed = parse_pdf(pdf_path)

    if not parsed['date'] and not parsed['title']:
        print("  ERROR: Could not detect meeting. Is this an FTH printed agenda PDF?")
        return

    preview_parsed(parsed, data)

    if not auto:
        confirm = input("\n  Add this meeting to data.json? (y/n): ").strip().lower()
        if confirm != 'y':
            print("  Skipped."); return
        manual = prompt_manual_roles(data)
        if manual:
            parsed['roles'].extend(manual)

    members = data['members']
    meeting_entry = {
        'date': parsed['date'] or '0000-00-00',
        'title': parsed['title'] or str(pdf_path.name),
        'attending': [],
        'notAttending': [],
        'roles': []
    }

    for n in parsed['attending']:
        matched = find_member(n, members)
        if matched: meeting_entry['attending'].append(matched)

    for n in parsed['not_attending']:
        matched = find_member(n, members)
        if matched: meeting_entry['notAttending'].append(matched)

    for r in parsed['roles']:
        matched = find_member(r['member'], members)
        if matched: meeting_entry['roles'].append({'role': r['role'], 'member': matched})

    existing_dates = [m['date'] for m in data.get('meetings', [])]
    if meeting_entry['date'] in existing_dates:
        print(f"\n  A meeting for {meeting_entry['date']} already exists.")
        if not auto:
            ans = input("  Overwrite? (y/n): ").strip().lower()
            if ans != 'y':
                print("  Skipped."); return
        data['meetings'] = [m for m in data['meetings'] if m['date'] != meeting_entry['date']]

    data.setdefault('meetings', []).append(meeting_entry)
    data['meetings'].sort(key=lambda m: m['date'])
    save_data(data)

    title = parsed['title']
    print(f"\n  data.json updated: {title} added ({len(meeting_entry['roles'])} roles, {len(meeting_entry['attending'])} attending)")
    print(f"  Next: git add data/data.json && git commit -m 'Add {title}' && git push")

def main():
    parser = argparse.ArgumentParser(description='Parse FTH agenda PDFs and update NVT tracker')
    parser.add_argument('path', help='PDF file or folder of PDFs')
    parser.add_argument('--auto', action='store_true', help='Skip confirmation prompts')
    args = parser.parse_args()
    target = Path(args.path)
    if target.is_dir():
        pdfs = sorted(target.glob('*.pdf'))
        if not pdfs:
            print(f"No PDFs found in {target}"); sys.exit(1)
        for pdf in pdfs:
            process_pdf(pdf, auto=args.auto)
    elif target.is_file():
        process_pdf(target, auto=args.auto)
    else:
        print(f"ERROR: '{target}' not found."); sys.exit(1)

if __name__ == '__main__':
    main()
