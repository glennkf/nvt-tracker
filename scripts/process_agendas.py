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
    'grammarian/word of the day report': None,
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
    'new member induction': None,
    'officer inductions': None,
    'officer induction': None,
    'role tracker prizes': None,
}

SKIP_CONTAINS = [
    'path / project', 'project title', 'evaluates the meeting',
    'impromptu speaking', 'evaluates the usage', 'opens meeting',
    'attending:', '(end) attending', 'not attending', 'meeting notes',
    'evaluated by:', 'evaluates:', 'open speaker', 'project guide',
    'eval form',
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
TRAIL_CRED = re.compile(
    r'^(DTM|IPE|ACB|ACS|ACG|ALB|ALS|CC|CL|CTM|MS|PM\d|EH\d|DL\d|PI\d|TC\d|VC\d|LD\d|SR\d)$',
    re.IGNORECASE
)
TIME_RE = re.compile(r'(\d{1,2}:\d{2}(?:AM|PM))', re.IGNORECASE)

# ─── NAME UTILS ───────────────────────────────────────────────────────────────
def clean_name(raw):
    if not raw or raw.strip() in ('-', '', '—', '–', '-', '- '): return None
    if 'open speaker' in raw.lower(): return None
    cleaned = CRED_RE.sub('', raw).strip().strip(',').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    # Credentials with no comma before them (wrapped lines): "Daniel J. Wallace DTM", "Michele Chamberlain VC5"
    toks = cleaned.split()
    while len(toks) > 2 and TRAIL_CRED.match(toks[-1]):
        toks.pop()
    cleaned = ' '.join(toks)
    if not cleaned or len(cleaned) < 3: return None
    if CRED_FRAG.match(cleaned): return None
    # Remove "(guest)" markers
    if '(guest)' in cleaned.lower(): return None
    return cleaned

PROJECT_LINE_RE = re.compile(
    r'^(?P<path>[A-Za-z][A-Za-z\' &]+?)\s+[—–-]\s+Level\s+(?P<lvl>\d)\s*:\s*(?P<proj>.+?)'
    r'\s*(?:\(\s*\d+\s*[–-]\s*\d+\s*min\s*\))?\s*$'
)
_SPEAKER_ROW_RE = re.compile(r'^\d{1,2}:\d{2}[AP]M\s+Speaker\b(.*)$', re.IGNORECASE)
_ANY_TIME_ROW_RE = re.compile(r'^\d{1,2}:\d{2}[AP]M\b')

def extract_speeches(text):
    """For each 'TIME Speaker <member>' row, collect the speech title and the
    Pathways project line that follow it, until the next time row.
    Returns [{'member', 'projectTitle', 'pathLevel'}] in agenda order.
    pathLevel uses the same format as the hand-entered history:
    'PATHWAYS - <Path>: (L<n>) <Project>'."""
    lines = [l.strip().lstrip('@ ').strip() if l.startswith('@@') else l.strip()
             for l in text.split('\n') if l.strip()]
    out = []
    i = 0
    while i < len(lines):
        m = _SPEAKER_ROW_RE.match(lines[i])
        if not m:
            i += 1
            continue
        member_raw = m.group(1).strip()
        member = clean_name(member_raw) if member_raw else None
        block = []
        j = i + 1
        while j < len(lines) and not _ANY_TIME_ROW_RE.match(lines[j]) \
                and not re.match(r'^(MEETING NOTES|Attending:|Not Attending:|https://)', lines[j], re.I):
            block.append(lines[j])
            j += 1
        title_parts, path_level, past_eval = [], None, False
        for b in block:
            low = b.lower()
            if low.startswith('evaluated by:'):
                past_eval = True
                continue
            if (low.startswith('project guide') or low.startswith('purpose of this speech')
                    or low.startswith('path / project')):
                break          # guide links / purpose text, or the older agenda template
            pm = PROJECT_LINE_RE.match(b)
            if pm:
                proj = re.sub(r'\s+', ' ', pm.group('proj')).strip()
                path_level = f"PATHWAYS - {pm.group('path').strip()}: (L{pm.group('lvl')}) {proj}"
                continue
            if low.startswith('custom speech'):
                continue
            if not past_eval:
                title_parts.append(b)
        title = ' '.join(title_parts).strip()
        title = title.strip('"').strip('“”').strip() or None
        out.append({'member': member, 'projectTitle': title, 'pathLevel': path_level})
        i = j
    return out

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
    r'Speaker(?:\s+#?\d+)?(?:\s*-\s*-)?(?:\s+\d+[-–]\d+\s*minutes?)?',
    r'General Evaluator',
    r'Evaluator(?:\s+#?\d+)?(?:\s*-\s*-)?(?:\s+\d+[-–]\d+\s*minutes?)?',
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
    r'New Member Induction',
    r'Officer Inductions?',
    r'Role Tracker Prizes',
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
    if not title_m:
        # FTH4 format: "Club Meeting\nSeptember 16, 2026 at 6:45PM EDT" (may wrap across lines)
        title_m = re.search(r'Club Meeting\s+([A-Za-z]+\s+\d+,?\s+\d{4})\s+at', text, re.IGNORECASE)
    title = title_m.group(1).strip() if title_m else filename
    date = None
    dm = re.search(
        r'(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d+),?\s+(\d{4})',
        title, re.IGNORECASE
    )
    if dm:
        date = f"{dm.group(3)}-{MONTHS[dm.group(1).lower()]}-{dm.group(2).zfill(2)}"

    # Fallback 1: first "Month D, YYYY" anywhere in the top of the document
    if not date:
        dm = re.search(
            r'(January|February|March|April|May|June|July|August|'
            r'September|October|November|December)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
            text[:1500], re.IGNORECASE
        )
        if dm:
            date = f"{dm.group(3)}-{MONTHS[dm.group(1).lower()]}-{dm.group(2).zfill(2)}"
            print(f'  Date taken from page header text: {date}')

    # Fallback 2: numeric date in the filename, e.g. "Printed Agenda 10-7-2026.pdf"
    if not date:
        fm = re.search(r'(\d{1,2})[-_.](\d{1,2})[-_.](\d{4})', filename)
        if fm:
            mo, dy, yr = int(fm.group(1)), int(fm.group(2)), fm.group(3)
            if 1 <= mo <= 12 and 1 <= dy <= 31:
                date = f"{yr}-{mo:02d}-{dy:02d}"
                print(f'  Date taken from filename: {date}')

    # Parse line by line - handles both "TIME ROLE MEMBER" and split lines
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    roles = []
    seen_times = set()
    i = 0
    stop_parsing = False

    while i < len(lines) and not stop_parsing:
        line = lines[i]

        # Stop at attending section
        if re.match(r'^(Attending:|Not Attending:|Meeting Notes?:?)', line, re.IGNORECASE):
            stop_parsing = True
            break

        # Check if line starts with a time marker
        tm = re.match(r'^(\d{1,2}:\d{2}(?:AM|PM))\s+(.*)', line, re.IGNORECASE)
        if not tm:
            i += 1
            continue

        time_key = tm.group(1)
        rest = tm.group(2).strip()
        # Strip FTH4 noise glued onto role lines (e.g. "Timer ⏱ Timer sheet Glenn Fernandes")
        rest = re.sub(r'⏱\s*Timer sheet\s*', '', rest, flags=re.IGNORECASE).strip()

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
    notes_block = re.search(r'Meeting Notes?:?(.*?)(?:https://|$)', text, re.DOTALL | re.IGNORECASE)
    if notes_block:
        notes = notes_block.group(1)

        # Table Topics Speakers: Name, Name, Name (may wrap across multiple lines)
        tt_m = re.search(
            r'Table Topics? Speakers?:\s*(.+?)(?=Introductory Mentor:|Club Mentor:|Specialized Role:|\n' + re.escape(AUTHOR_MARK.strip()) + r'|$)',
            notes, re.IGNORECASE | re.DOTALL
        )
        if tt_m:
            # Line-wrap within the list is not a delimiter - rejoin with a space, not a comma
            joined = re.sub(r'\s*\n\s*', ' ', tt_m.group(1)).strip()
            # Label sometimes repeats ("Table Topic Speakers: Table Topic Speakers: ...")
            joined = re.sub(r'^(?:Table Topics? Speakers?:\s*)+', '', joined, flags=re.IGNORECASE)
            for name in joined.split(','):
                mc = clean_name(name.strip())
                if not mc:
                    continue
                mc = re.sub(r'\s+[-–]\s+', '-', mc)   # "Karen Morway - Adams" -> "Karen Morway-Adams"
                if not looks_like_person_name(mc):
                    print(f'  Skipping non-name text in Table Topics list: {mc!r}')
                    continue
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

    # Attach speech title + Pathways project to each Speaker role (FTH4 layout)
    try:
        speeches = extract_speeches(text)
        speaker_roles = [r for r in roles if r['role'] == 'Speaker']
        used = set()
        for r in speaker_roles:
            for k, sp in enumerate(speeches):
                if k in used:
                    continue
                if sp['member'] and sp['member'].lower() == r['member'].lower():
                    used.add(k)
                    if sp['projectTitle']: r['projectTitle'] = sp['projectTitle']
                    if sp['pathLevel']: r['pathLevel'] = sp['pathLevel']
                    break
        # Fallback: same count of speaker rows on both sides -> pair by order
        if len(speaker_roles) == len(speeches) and not used:
            for r, sp in zip(speaker_roles, speeches):
                if sp['projectTitle']: r['projectTitle'] = sp['projectTitle']
                if sp['pathLevel']: r['pathLevel'] = sp['pathLevel']
    except Exception as e:
        print(f'  (could not read speech details: {e})')

    # Parse attending / not attending
    attending = []
    not_attending = []

    a_block = re.search(
        r'(?<!Not )Attending:\s*(.+?)(?=Not Attending:|Meeting Notes?:?|https://|$)',
        text, re.DOTALL | re.IGNORECASE
    )
    if a_block:
        for name in a_block.group(1).replace('\n', ',').split(','):
            c = clean_name(name.strip())
            if c and len(c.split()) >= 2 and not CRED_FRAG.match(c):
                attending.append(c)

    na_block = re.search(
        r'Not Attending:\s*(.+?)(?=Meeting Notes?:?|https://|$)',
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

    with pdfplumber.open(fh) as pdf:
        return pdf_to_text(pdf)

AUTHOR_MARK = '@@ '

_NAME_PARTICLES = {'de', 'da', 'del', 'van', 'von', 'der', 'la', 'le', 'di', 'bin', 'al', 'st.'}

def looks_like_person_name(n):
    """Safety net: a Table Topics entry must look like a person's name
    (1 to 5 words, no digits, each word capitalised or a name particle)."""
    toks = n.split()
    if not 1 <= len(toks) <= 5:
        return False
    for t in toks:
        if any(ch.isdigit() for ch in t):
            return False
        if t.lower() in _NAME_PARTICLES:
            continue
        if not re.match(r"^[A-Z][A-Za-z.'’\-]*$", t):
            return False
    return True

def marked_text(page, y0):
    """Text from y0 down, with bold lines (note author signatures in the
    Meeting Notes area) prefixed by AUTHOR_MARK. Each note in FTH starts with
    a bold author line, so this cleanly separates one note from the next."""
    region = page.crop((0, y0, page.width, page.height))
    words = region.extract_words(extra_attrs=['fontname', 'size'])
    ws = sorted(words, key=lambda w: (round(w['top']), w['x0']))
    lines, cur, ct = [], [], None
    for w in ws:
        if ct is None or abs(w['top'] - ct) > 3:
            if cur: lines.append(cur)
            cur, ct = [w], w['top']
        else:
            cur.append(w)
    if cur: lines.append(cur)
    out = []
    for ln in lines:
        txt = ' '.join(w['text'] for w in ln)
        bold = all('bold' in w['fontname'].lower() for w in ln) and min(w['size'] for w in ln) >= 9.5
        out.append((AUTHOR_MARK + txt) if bold else txt)
    return '\n'.join(out)

def plain_text_marked(page):
    """Plain page text, but with the Meeting Notes area bold-marked."""
    try:
        ws = page.extract_words()
        for idx, w in enumerate(ws):
            nxt = ws[idx + 1]['text'] if idx + 1 < len(ws) else ''
            if w['text'] == 'MEETING' and nxt.upper().startswith('NOTES'):
                y = w['top'] - 1
                head = page.crop((0, 0, page.width, max(y, 1))).extract_text() or ''
                return head + '\n' + marked_text(page, y)
    except Exception:
        pass
    return page.extract_text() or ''

def page_to_text(page):
    """Rebuild agenda rows by column position so wrapped member names stay
    on the same line as their role (e.g. 'Toastmaster Maureen DeRoma').
    Falls back to plain extract_text() if the layout isn't recognised."""
    plain = page.extract_text() or ""
    try:
        words = page.extract_words()
        anchors = [w for w in words if re.fullmatch(r'\d{1,2}:\d{2}[AP]M', w['text']) and w['x0'] < 70]
        mem_hdr = [w for w in words if w['text'] == 'Member' and w['x0'] > 400]
        if not anchors or not mem_hdr:
            return plain_text_marked(page)
        mx = mem_hdr[0]['x0'] - 5
        anchors.sort(key=lambda w: w['top'])
        # cutoff where free text begins (meeting notes / attending / footer)
        cut = page.height
        for idx, w in enumerate(words):
            nxt = words[idx + 1]['text'] if idx + 1 < len(words) else ''
            if w['top'] > anchors[0]['top'] and (
                (w['text'].upper() == 'MEETING' and nxt.upper().startswith('NOTES')) or
                w['text'] in ('Attending:', 'Not') or w['text'].startswith('https://')):
                cut = min(cut, w['top'] - 1)
        head = page.crop((0, 0, page.width, max(anchors[0]['top'] - 2, 1))).extract_text() or ''
        out = [head]
        for k, a in enumerate(anchors):
            top = a['top'] - 2
            bot = (anchors[k + 1]['top'] - 2) if k + 1 < len(anchors) else cut
            left = [w for w in words if top <= w['top'] < bot and w['x0'] < mx]
            right = [w for w in words if top <= w['top'] < bot and w['x0'] >= mx]
            def to_lines(ws):
                ws = sorted(ws, key=lambda w: (round(w['top']), w['x0']))
                lines, cur, ct = [], [], None
                for w in ws:
                    if ct is None or abs(w['top'] - ct) > 3:
                        if cur: lines.append(' '.join(cur))
                        cur, ct = [w['text']], w['top']
                    else:
                        cur.append(w['text'])
                if cur: lines.append(' '.join(cur))
                return lines
            ll, rl = to_lines(left), ' '.join(to_lines(right))
            if not ll:
                continue
            out.append((ll[0] + ' ' + rl).strip())
            out.extend(ll[1:])
        if cut < page.height:
            out.append(marked_text(page, cut))
        return '\n'.join(out)
    except Exception as e:
        print(f'  (column extraction failed: {e}; using plain text)')
        return plain

def pdf_to_text(pdf, filename=''):
    plain = ''.join(plain_text_marked(p) + '\n' for p in pdf.pages)
    cols = ''.join(page_to_text(p) + '\n' for p in pdf.pages)
    try:
        n_plain = len(parse_agenda_text(plain, filename)['roles'])
        n_cols = len(parse_agenda_text(cols, filename)['roles'])
    except Exception:
        return plain
    # Use column-aware text only when it finds at least as many roles
    return cols if n_cols >= n_plain else plain

def fill_speech_details(data, parsed, members):
    """For a meeting already in data.json, add projectTitle / pathLevel to Speaker
    roles that have neither. Never overwrites anything already stored, so manual
    edits are safe. Returns the number of roles updated."""
    meeting = next((m for m in data['meetings'] if m['date'] == parsed['date']), None)
    if not meeting:
        return 0
    filled = 0
    for pr in parsed['roles']:
        if pr['role'] != 'Speaker' or not (pr.get('projectTitle') or pr.get('pathLevel')):
            continue
        who = find_member(pr['member'], members) or pr['member']
        for sr in meeting['roles']:
            if sr['role'] == 'Speaker' and sr['member'] == who \
                    and not sr.get('projectTitle') and not sr.get('pathLevel'):
                if pr.get('projectTitle'): sr['projectTitle'] = pr['projectTitle']
                if pr.get('pathLevel'): sr['pathLevel'] = pr['pathLevel']
                filled += 1
                print(f'  + Speech details added: {who} ({parsed["date"]})')
                break
    return filled

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
    details_filled = 0

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
                details_filled += fill_speech_details(data, parsed, members)
                continue
            elif stored_modified >= current_modified:
                print(f'  {parsed["date"]} already in tracker and PDF unchanged — skipping')
                details_filled += fill_speech_details(data, parsed, members)
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
            new_role = {'role': r['role'], 'member': matched}
            if r.get('projectTitle'): new_role['projectTitle'] = r['projectTitle']
            if r.get('pathLevel'): new_role['pathLevel'] = r['pathLevel']
            entry['roles'].append(new_role)

        data['meetings'].append(entry)
        existing_dates.add(parsed['date'])
        new_meetings += 1
        print(f'  ✓ Added to tracker')

    # Sort meetings by date
    data['meetings'].sort(key=lambda m: m['date'])

    if details_filled:
        print(f'\n  Filled speech details for {details_filled} existing speaker role(s)')

    if new_meetings > 0 or new_members_added or details_filled:
        with open(DATA_PATH, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f'\n✓ data.json updated:')
        print(f'  {new_meetings} new meeting(s) added')
        print(f'  {len(new_members_added)} new member(s) added: {new_members_added}')
    else:
        print('\n✓ No new meetings found — data.json unchanged')

if __name__ == '__main__':
    main()
