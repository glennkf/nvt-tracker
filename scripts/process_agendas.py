#!/usr/bin/env python3
"""
NVT Agenda Processor — GitHub Actions version
Reads PDFs from Google Drive NVT Agendas folder, parses roles/attendance,
updates data/data.json. Runs automatically via GitHub Actions.
"""

import json
import os
import re
import base64
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── CONFIG ───────────────────────────────────────────────────────────────────
FOLDER_ID = '1e4OyBt7gu544qurF6W4j5PFSCvVLlrJG'
DATA_PATH = Path(__file__).parent.parent / 'data' / 'data.json'

MONTHS = {
    'january':'01','february':'02','march':'03','april':'04',
    'may':'05','june':'06','july':'07','august':'08',
    'september':'09','october':'10','november':'11','december':'12'
}

ROLE_ALIASES = {
    'grammarian/word of the day': 'Grammarian',
    'grammarian/word of the day report': 'Grammarian',
    'ah-counter': 'Grammarian', 'ah counter': 'Grammarian',
    'evaluator #1': 'Evaluator', 'evaluator #2': 'Evaluator',
    'evaluator #3': 'Evaluator', 'evaluator #4': 'Evaluator',
    'speaker #1': 'Speaker', 'speaker #2': 'Speaker',
    'speaker #3': 'Speaker', 'speaker #4': 'Speaker',
    'table topics speaker': 'Table Topics Speaker',
    "president's welcome": None, 'presidents welcome': None,
    "president's closing remarks": None, 'presidents closing remarks': None,
    'secretary/role call': None, 'secretary': None,
    'sergeant at arms': None,
    'toastmaster return from break': None, 'toastmasters return from break': None,
    'return from break': None, 'return from break –': None,
    'timer report': None, 'voting results': None, 'voting for officers': None,
    'best speaker/evaluator': None, 'break': None,
    '(end) attending:': None, 'attending:': None,
    'not attending:': None,
    'new member ice breaker speech #1': 'Speaker',
    'new member ice breaker speech #2': 'Speaker',
    'new member ice breaker speech #3': 'Speaker',
    'new member ice breaker speech': 'Speaker',
}

SKIP_CONTAINS = [
    'path / project', 'project title', 'evaluates the meeting',
    'impromptu speaking', 'evaluates the usage', 'opens meeting',
    'attending:', '(end) attending', 'not attending', 'meeting notes'
]

CRED_RE = re.compile(
    r',\s*(DTM|IPE|ACB|ACS|ACG|ALB|ALS|CC|CL|CTM|TM|'
    r'Pathways[\s\w]+\d*|MS\d*|VC\d*|LD\d*|SR\d*)[^,]*',
    re.IGNORECASE
)
CRED_FRAG = re.compile(
    r'^(DTM|ACB|MS\d*|VC\d*|LD\d*|SR\d*|Pathways\s+\w+|Pathways\s+Mentor|CC|CL)$',
    re.IGNORECASE
)
TIME_RE = re.compile(r'(\d{1,2}:\d{2}(?:AM|PM))', re.IGNORECASE)

# ─── NAME UTILS ───────────────────────────────────────────────────────────────
def clean_name(raw):
    if not raw or raw.strip() in ('-', '', '—', '–', '-', '- '): return None
    cleaned = CRED_RE.sub('', raw).strip().strip(',').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if not cleaned or len(cleaned) < 3: return None
    if CRED_FRAG.match(cleaned): return None
    # Remove "(guest)" markers
    if '(guest)' in cleaned.lower(): return None
    return cleaned

def normalize_role(r, from_notes=False):
    if not r: return None
    r = r.replace('\\', '').strip()
    # Remove duration patterns like "- - 10-15 minutes"
    r = re.sub(r'[-–]\s*[-–]\s*\d+[-–]\d+\s*minutes?', '', r, flags=re.IGNORECASE).strip()
    low = r.lower()
    # Table Topics Speaker as agenda role = Table Topics Master (not from Meeting Notes)
    if low == 'table topics speaker' and not from_notes:
        return 'Table Topics Master'
    for k, v in ROLE_ALIASES.items():
        if k == low: return v
    if any(s in low for s in SKIP_CONTAINS): return None
    if low.startswith('https://'): return None
    if TIME_RE.match(r): return None
    if not r: return None
    return r

def find_member(name, members):
    if not name: return None
    # Normalize hyphens to spaces for matching
    low = name.lower().replace('-', ' ')
    # Pass 1: exact match
    for m in members:
        mlow = m['name'].lower().replace('-', ' ')
        if mlow == low: return m['name']
    # Pass 2: last name + first 3 chars of first name (avoids M. O'Connor ambiguity)
    parts = low.split()
    for m in members:
        mp = m['name'].lower().replace('-', ' ').split()
        if (len(parts) >= 2 and len(mp) >= 2 and
            mp[-1] == parts[-1] and
            len(parts[0]) >= 3 and len(mp[0]) >= 3 and
            mp[0][:3] == parts[0][:3]):
            return m['name']
    # Pass 3: last name + first initial (only if no ambiguity - last name is unique)
    last_name_counts = {}
    for m in members:
        last = m['name'].lower().replace('-',' ').split()[-1]
        last_name_counts[last] = last_name_counts.get(last, 0) + 1
    for m in members:
        mp = m['name'].lower().replace('-', ' ').split()
        if (len(parts) >= 2 and len(mp) >= 2 and
            mp[-1] == parts[-1] and
            mp[0][0] == parts[0][0] and
            last_name_counts.get(parts[-1], 0) == 1):  # only if last name is unique
            return m['name']
    # Pass 4: substring match
    for m in members:
        mlow = m['name'].lower().replace('-', ' ')
        if low in mlow or mlow in low:
            return m['name']
    return None

# ─── PARSE AGENDA TEXT ────────────────────────────────────────────────────────
KNOWN_ROLES = [
    r'Thought of the day',
    r'Toastmaster(?!s?\s+return)',
    r'Joke of the day',
    r'Timer(?!\s+Report)',
    r'Grammarian/Word of the Day(?:\s+Report)?',
    r'Speaker\s+#?\d+(?:\s*-\s*-)?(?:\s+\d+[-–]\d+\s*minutes?)?',
    r'General Evaluator',
    r'Evaluator\s+#?\d+(?:\s*-\s*-)?(?:\s+\d+[-–]\d+\s*minutes?)?',
    r'Table Topics Master',
    r'Table Topics Speaker',
    r"President'?s?\s+(?:Welcome|Closing Remarks)",
    r'Sergeant at [Aa]rms',
    r'Secretary/role call',
    r'Best Speaker/Evaluator',
    r'Timer Report',
    r'Voting(?:\s+for\s+Officers|\s+Results)?',
    r'Break',
    r'Toastmasters? return from break',
]
KNOWN_ROLE_RE = re.compile(
    r'^(' + '|'.join(KNOWN_ROLES) + r')\b', re.IGNORECASE
)

def parse_agenda_text(text, filename=''):
    text = (text
        .replace('\\!', '!').replace('\\&', '&')
        .replace('\\-', '-').replace('\\#', '#'))

    # Extract meeting date from title line
    title_m = re.search(r'Agenda Item for ([\w]+\s+\d+,?\s+\d{4})', text, re.IGNORECASE)
    title = title_m.group(1).strip() if title_m else filename
    date = None
    dm = re.search(
        r'(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d+),?\s+(\d{4})',
        title, re.IGNORECASE
    )
    if dm:
        date = f"{dm.group(3)}-{MONTHS[dm.group(1).lower()]}-{dm.group(2).zfill(2)}"

    # Parse line by line - handles both "TIME ROLE MEMBER" and split lines
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    roles = []
    seen_times = set()
    i = 0
    stop_parsing = False

    while i < len(lines) and not stop_parsing:
        line = lines[i]

        # Stop at attending section
        if re.match(r'^(Attending:|Not Attending:|Meeting Notes?:)', line, re.IGNORECASE):
            stop_parsing = True
            break

        # Check if line starts with a time marker
        tm = re.match(r'^(\d{1,2}:\d{2}(?:AM|PM))\s+(.*)', line, re.IGNORECASE)
        if not tm:
            i += 1
            continue

        time_key = tm.group(1)
        rest = tm.group(2).strip()

        if time_key in seen_times:
            i += 1
            continue
        seen_times.add(time_key)

        # Skip end times like "8:31PM (end)"
        if rest.startswith('(end)') or rest.startswith('('):
            i += 1
            continue

        # Remove duration patterns
        rest = re.sub(r'[-–]\s*[-–]\s*\d+[-–]\d+\s*minutes?', '', rest, flags=re.IGNORECASE).strip()

        # Stop at attending section in rest
        if any(s in rest.lower() for s in ['attending:', 'not attending:', 'meeting notes']):
            stop_parsing = True
            break

        # Skip descriptions
        if any(s in rest.lower() for s in SKIP_CONTAINS):
            i += 1
            continue

        # Try to match known role at start
        role_raw = None
        member_raw = None

        m = KNOWN_ROLE_RE.match(rest)
        if m:
            role_raw = m.group(1).strip()
            member_raw = rest[m.end():].strip()
        else:
            # Fallback split
            nm = re.match(r'^(.+?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z.\']+)+.*?)$', rest)
            if nm:
                role_raw = nm.group(1)
                member_raw = nm.group(2)
            else:
                role_raw = rest
                member_raw = None

        # If no member found on this line, check next non-description line
        if not member_raw or not member_raw.strip() or member_raw.strip() in ('-', '–', '—'):
            # Look ahead for member name on next line
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                # Stop if next line starts with a time (new role entry)
                if re.match(r'^\d{1,2}:\d{2}(?:AM|PM)', next_line, re.IGNORECASE):
                    break
                # Skip description lines
                if any(s in next_line.lower() for s in SKIP_CONTAINS):
                    j += 1
                    continue
                # Skip path/project lines
                if re.match(r'^(Path|Project Title|Evaluates|Impromptu|https://)', next_line, re.IGNORECASE):
                    j += 1
                    continue
                # Stop at attending
                if re.match(r'^(Attending:|Not Attending:)', next_line, re.IGNORECASE):
                    break
                # This looks like a member name
                candidate = clean_name(next_line)
                if candidate and len(candidate.split()) >= 2:
                    member_raw = next_line
                    break
                j += 1

        nr = normalize_role(role_raw)
        if nr is None:
            i += 1
            continue

        mc = clean_name(member_raw) if member_raw else None
        if mc:
            roles.append({'role': nr, 'member': mc})

        i += 1

    # Parse Meeting Notes for Table Topics Speakers, Mentors, etc.
    notes_block = re.search(r'Meeting Notes?:(.*?)(?:https://|$)', text, re.DOTALL | re.IGNORECASE)
    if notes_block:
        notes = notes_block.group(1)

        # Table Topics Speakers: Name, Name, Name
        tt_m = re.search(r'Table Topics? Speakers?:\s*(.+?)(?:\n|$)', notes, re.IGNORECASE)
        if tt_m:
            for name in tt_m.group(1).split(','):
                mc = clean_name(name.strip())
                if mc:
                    roles.append({'role': normalize_role('Table Topics Speaker', from_notes=True), 'member': mc})

        # Introductory Mentor: Name
        im_m = re.search(r'Introductory Mentor:\s*(.+?)(?:\n|$)', notes, re.IGNORECASE)
        if im_m:
            mc = clean_name(im_m.group(1).strip())
            if mc:
                roles.append({'role': 'Introductory Mentor', 'member': mc})

        # Club Mentor: Name
        cm_m = re.search(r'Club Mentor:\s*(.+?)(?:\n|$)', notes, re.IGNORECASE)
        if cm_m:
            mc = clean_name(cm_m.group(1).strip())
            if mc:
                roles.append({'role': 'Club Mentor', 'member': mc})

        # Specialized Role: Name — Description
        sr_m = re.search(r'Specialized Role:\s*(.+?)(?:\n|$)', notes, re.IGNORECASE)
        if sr_m:
            mc = clean_name(sr_m.group(1).split('—')[0].strip())
            if mc:
                roles.append({'role': 'Specialized Role', 'member': mc})

    # Parse attending / not attending
    attending = []
    not_attending = []

    a_block = re.search(
        r'(?<!Not )Attending:\s*(.+?)(?=Not Attending:|Meeting Notes?:|https://|$)',
        text, re.DOTALL | re.IGNORECASE
    )
    if a_block:
        for name in a_block.group(1).replace('\n', ',').split(','):
            c = clean_name(name.strip())
            if c and len(c.split()) >= 2 and not CRED_FRAG.match(c):
                attending.append(c)

    na_block = re.search(
        r'Not Attending:\s*(.+?)(?=Meeting Notes?:|https://|$)',
        text, re.DOTALL | re.IGNORECASE
    )
    if na_block:
        for name in na_block.group(1).replace('\n', ',').split(','):
            c = clean_name(name.strip())
            if c and len(c.split()) >= 2 and not CRED_FRAG.match(c):
                not_attending.append(c)

    return {
        'date': date,
        'title': title,
        'roles': roles,
        'attending': list(dict.fromkeys(attending)),
        'not_attending': list(dict.fromkeys(not_attending)),
    }

# ─── GOOGLE DRIVE ─────────────────────────────────────────────────────────────
def get_drive_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if not creds_json:
        raise ValueError('GOOGLE_CREDENTIALS environment variable not set')

    creds_data = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_data,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds)

def list_pdfs(service):
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false",
        fields='files(id, name, modifiedTime)',
        orderBy='name'
    ).execute()

    return results.get('files', [])

def read_pdf_text(service, file_id):
    import io
    import pdfplumber
    from googleapiclient.http import MediaIoBaseDownload

    # Download raw PDF bytes
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)

    # Extract text using pdfplumber
    text = ""
    with pdfplumber.open(fh) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print('Loading data.json...')
    with open(DATA_PATH) as f:
        data = json.load(f)

    existing_dates = {m['date'] for m in data.get('meetings', [])}
    existing_modified = {m['date']: m.get('pdfModifiedTime', '') for m in data.get('meetings', [])}
    members = data['members']

    print('Connecting to Google Drive...')
    service = get_drive_service()

    print(f'Scanning folder {FOLDER_ID}...')
    pdfs = list_pdfs(service)
    print(f'Found {len(pdfs)} PDF(s) in NVT Agendas folder')

    new_meetings = 0
    new_members_added = []

    for pdf in pdfs:
        print(f'\nProcessing: {pdf["name"]}')

        try:
            text = read_pdf_text(service, pdf['id'])
        except Exception as e:
            print(f'  Could not read {pdf["name"]}: {e}')
            continue

        parsed = parse_agenda_text(text, pdf['name'])

        if not parsed['date']:
            print(f'  Could not detect date — skipping')
            continue

        if parsed['date'] in existing_dates:
            stored_modified = existing_modified.get(parsed['date'], '')
            current_modified = pdf.get('modifiedTime', '')
            # Only reprocess if we have a stored modifiedTime AND PDF has changed since
            # If no stored modifiedTime (legacy meetings), skip as normal to protect manual fixes
            if not stored_modified:
                print(f'  {parsed["date"]} already in tracker — skipping')
                continue
            elif stored_modified >= current_modified:
                print(f'  {parsed["date"]} already in tracker and PDF unchanged — skipping')
                continue
            else:
                print(f'  {parsed["date"]} PDF was modified — reprocessing')
                data['meetings'] = [m for m in data['meetings'] if m['date'] != parsed['date']]
                existing_dates.discard(parsed['date'])

        print(f'  Date: {parsed["date"]}')
        print(f'  Roles found: {len(parsed["roles"])}')
        for r in parsed['roles']:
            print(f'    {r["role"]:<30} {r["member"]}')

        # Build meeting entry
        entry = {
            'date': parsed['date'],
            'title': parsed['title'],
            'pdfModifiedTime': pdf.get('modifiedTime', ''),
            'attending': [],
            'notAttending': [],
            'roles': []
        }

        for name in parsed['attending']:
            matched = find_member(name, members)
            if matched:
                entry['attending'].append(matched)

        for name in parsed['not_attending']:
            matched = find_member(name, members)
            if matched:
                entry['notAttending'].append(matched)

        for r in parsed['roles']:
            matched = find_member(r['member'], members)
            if not matched:
                # Auto-add new member
                new_member = {'name': r['member'], 'active': True, 'manualRoles': []}
                members.append(new_member)
                matched = r['member']
                new_members_added.append(r['member'])
                print(f'  + New member added: {r["member"]}')
            entry['roles'].append({'role': r['role'], 'member': matched})

        data['meetings'].append(entry)
        existing_dates.add(parsed['date'])
        new_meetings += 1
        print(f'  ✓ Added to tracker')

    # Sort meetings by date
    data['meetings'].sort(key=lambda m: m['date'])

    if new_meetings > 0 or new_members_added:
        with open(DATA_PATH, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f'\n✓ data.json updated:')
        print(f'  {new_meetings} new meeting(s) added')
        print(f'  {len(new_members_added)} new member(s) added: {new_members_added}')
    else:
        print('\n✓ No new meetings found — data.json unchanged')

if __name__ == '__main__':
    main()
