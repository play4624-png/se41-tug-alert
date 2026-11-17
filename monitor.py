import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = 'http://ystug.co.kr/sub07/sub03_01.php'
STATE = Path('state.json')
KST = ZoneInfo('Asia/Seoul')
TUG_NAME = '서41'
FIELDS = ['선박명', '총톤수', '흘수', '요청일시', '시작점', '종료점', 'Work', 'B', 'B/T', '배정예선']
CHANGE_FIELDS = ['요청일시', '시작점', '종료점', 'Work', 'B', 'B/T']
TUG = re.compile(r'(?<![가-힣A-Za-z0-9])서\s*41(?![가-힣A-Za-z0-9])')
COMMANDS = {
    '📋 오늘배정': 'today', '📅 내일배정': 'tomorrow', '📑 전체배정': 'all',
    '/오늘배정': 'today', '/현재배정': 'today', '/내일배정': 'tomorrow', '/전체배정': 'all'
}
KEYBOARD = {
    'keyboard': [[{'text': '📋 오늘배정'}, {'text': '📅 내일배정'}, {'text': '📑 전체배정'}]],
    'resize_keyboard': True, 'is_persistent': True
}


def parse_rows(html, day):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.wover_table')
    if table is None:
        raise RuntimeError('배정표를 찾지 못했습니다.')
    headings = [x.get_text(' ', strip=True) for x in table.select('thead th')]
    if not all(x in headings for x in FIELDS):
        raise RuntimeError(f'배정표 열 구성이 변경되었습니다: {headings}')
    grouped = defaultdict(list)
    for tr in table.select('tbody tr'):
        cells = [x.get_text(' ', strip=True) for x in tr.find_all('td', recursive=False)]
        if len(cells) != len(headings):
            continue
        row = dict(zip(headings, cells))
        try:
            req = datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M')
        except ValueError:
            continue
        if req.strftime('%Y%m%d') != day:
            continue
        row = {k: row.get(k, '') for k in FIELDS}
        key = '|'.join(row[k] for k in ['선박명', '요청일시', '시작점', '종료점'])
        grouped[key].append(row)
    rows = {}
    for key, entries in grouped.items():
        ordered = sorted(entries, key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
        for i, row in enumerate(ordered):
            rows[f'{key}|#{i+1}'] = row
    if not rows:
        raise RuntimeError(f'{day}: 해당 날짜의 행이 없습니다. 오탐 방지를 위해 중단합니다.')
    return rows


def fetch_rows(day):
    response = requests.post(
        URL,
        data={'p_dt_req': day, 'p_txt_ship': '', 'p_nm_bizarea': '', 'p_nm_tugboat': ''},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': URL},
        timeout=30
    )
    response.raise_for_status()
    detected = response.apparent_encoding
    response.encoding = detected if detected.lower() != 'ascii' else 'utf-8'
    return parse_rows(response.text, day)


def tug_names(row):
    if not row:
        return []
    return [re.sub(r'\s+', '', x) for x in re.split(r'[,，、/;\n]+', row.get('배정예선', '')) if x.strip()]


def assigned(row):
    return bool(row and TUG.search(row.get('배정예선', '')))


def request_time(row):
    try:
        return datetime.strptime(row['요청일시'], '%Y/%m/%d %H:%M').replace(tzinfo=KST)
    except (KeyError, ValueError):
        return None


def eligible(row, now):
    req = request_time(row)
    return bool(req and now.date() <= req.date() <= (now + timedelta(days=1)).date()
                and now < req + timedelta(hours=3))


def ship(row):
    return ' '.join(row['선박명'].upper().split())


def match_rows(old_rows, new_rows):
    old_remaining = dict(old_rows)
    new_remaining = dict(new_rows)
    pairs = []

    def identity(row):
        return tuple(row.get(k, '') for k in ('선박명', '요청일시', '시작점', '종료점'))

    old_groups = defaultdict(list)
    new_groups = defaultdict(list)
    for k, row in old_remaining.items():
        old_groups[identity(row)].append(k)
    for k, row in new_remaining.items():
        new_groups[identity(row)].append(k)
    for ident in old_groups.keys() & new_groups.keys():
        oks, nks = old_groups[ident], new_groups[ident]
        if len(oks) == len(nks) == 1:
            pairs.append((old_remaining.pop(oks[0]), new_remaining.pop(nks[0])))

    old_names = defaultdict(list)
    new_names = defaultdict(list)
    for k, row in old_remaining.items():
        old_names[ship(row)].append(k)
    for k, row in new_remaining.items():
        new_names[ship(row)].append(k)
    for name in old_names.keys() | new_names.keys():
        oks, nks = old_names.get(name, []), new_names.get(name, [])
        if len(oks) == len(nks) == 1:
            pairs.append((old_remaining.pop(oks[0]), new_remaining.pop(nks[0])))
        elif not oks:
            pairs.extend((None, new_remaining.pop(k)) for k in nks)
        elif not nks:
            pairs.extend((old_remaining.pop(k), None) for k in oks)
        else:
            print(f'주의: {name} 동일 선박 복수 작업 매칭 불명확. 알림 보류')
    return pairs


def tug_changes(old, new):
    before = tug_names(old)
    after = tug_names(new)
    if Counter(before) == Counter(after):
        return None
    if not before:
        return None
    before_text = ', '.join(before)
    after_text = ', '.join(after) if after else '미배정'
    return f'🔄 예선 변경: {before_text} → {after_text}'


def describe(row):
    return (
        f"{row['선박명']} | {row['총톤수']}톤 | 흘수 {row['흘수']}\n"
        f"요청일시 {row['요청일시']}\n"
        f"작업구간 {row['시작점']} → {row['종료점']}\n"
        f"Work {row['Work']} | B {row['B']} | B/T {row['B/T']}\n"
        f"배정예선 {row['배정예선']}"
    )


def telegram_send(token, chat_id, text, keyboard=False):
    data = {'chat_id': chat_id, 'text': text}
    if keyboard:
        data['reply_markup'] = KEYBOARD
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json=data, timeout=20)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('텔레그램 전송 실패')


def send(token, chat_id, kind, row, old=None, new=None):
    icon = {'신규배정': '🟢', '작업변경': '🔵', '배정취소': '🟠', '작업취소': '🔴'}[kind]
    lines = [f'{icon} [{kind}] {TUG_NAME}', describe(row)]
    if kind == '작업취소':
        lines.append('❌ 작업 자체가 배정표에서 삭제됨')
    if old and new:
        for field in CHANGE_FIELDS:
            if old.get(field, '') != new.get(field, ''):
                label = '🕒 시간 변경' if field == '요청일시' else f'✏️ {field} 변경'
                lines.append(f"{label}: {old.get(field) or '-'} → {new.get(field) or '-'}")
        changes = tug_changes(old, new)
        if changes:
            lines.append(changes)
    telegram_send(token, chat_id, '\n'.join(lines))


def read_state():
    if not STATE.exists():
        return {}, {}, None
    previous = json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(previous, dict):
        return {}, {}, None
    days_data = previous.get('days')
    previous_by_day = {day: rows for day, rows in days_data.items() if isinstance(rows, dict)} if isinstance(days_data, dict) else {}
    offset = previous.get('telegram_offset')
    if not isinstance(offset, int) or offset < 0:
        offset = None
    return previous, previous_by_day, offset


def get_updates(token, offset):
    params = {'timeout': 0, 'limit': 100, 'allowed_updates': json.dumps(['message'])}
    if offset is not None:
        params['offset'] = offset
    response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get('ok'):
        raise RuntimeError('텔레그램 명령어 조회 실패')
    return payload.get('result', [])


def command_reply(command, current_by_day, now):
    today = now.strftime('%Y%m%d')
    tomorrow = (now + timedelta(days=1)).strftime('%Y%m%d')
    mode = COMMANDS[command]
    target_days = [today] if mode == 'today' else [tomorrow] if mode == 'tomorrow' else [today, tomorrow]
    selected = []
    for day in target_days:
        for row in current_by_day[day].values():
            req = request_time(row)
            if assigned(row) and req:
                selected.append((req, row))
    selected.sort(key=lambda item: (item[0], item[1]['선박명']))
    title = {'today': '오늘 배정 전체', 'tomorrow': '내일 배정', 'all': '오늘·내일 배정 전체'}[mode]
    header = f'🚢 {TUG_NAME} {title}\n조회시각: {now:%Y/%m/%d %H:%M} (KST)\n총 {len(selected)}건'
    if not selected:
        return [header + '\n현재 조건에 해당하는 배정 작업이 없습니다.']
    messages = []
    buf = header
    for index, (_, row) in enumerate(selected, 1):
        entry = f'\n\n[{index}] {describe(row)}'
        if len(buf) + len(entry) > 3500:
            messages.append(buf)
            buf = f'🚢 {TUG_NAME} {title} (계속)\n' + entry.lstrip()
        else:
            buf += entry
    messages.append(buf)
    return messages


def process_commands(token, chat_id, current_by_day, now, offset):
    updates = get_updates(token, offset)
    new_offset = offset
    for update in updates:
        update_id = update.get('update_id')
        if not isinstance(update_id, int):
            continue
        message = update.get('message') or {}
        text = (message.get('text') or '').strip()
        command = text if text in COMMANDS else text.split(maxsplit=1)[0].split('@', 1)[0] if text else ''
        if str(message.get('chat', {}).get('id')) == str(chat_id) and command in COMMANDS:
            for reply in command_reply(command, current_by_day, now):
                telegram_send(token, chat_id, reply)
            print(f'텔레그램 명령어 처리: {command}')
        new_offset = update_id + 1
    return new_offset


def save_state(days, offset, keyboard_ready=False):
    payload = {'monitored_tug': TUG_NAME, 'days': days, 'keyboard_ready': keyboard_ready}
    if offset is not None:
        payload['telegram_offset'] = offset
    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    now = datetime.now(KST)
    days = [now.strftime('%Y%m%d'), (now + timedelta(days=1)).strftime('%Y%m%d')]
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('GitHub Secrets에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID가 필요합니다.')
    current_by_day = {day: fetch_rows(day) for day in days}
    previous_state, previous_by_day, offset = read_state()
    keyboard_ready = previous_state.get('keyboard_ready') is True
    if previous_state.get('monitored_tug') != TUG_NAME:
        print('서41 최초 적용: 기준 데이터 새로 저장')
        previous_by_day = {}
    for day in days:
        current = current_by_day[day]
        old_rows = previous_by_day.get(day)
        if old_rows is None:
            print(f'{day}: 서41 기준 데이터 최초 저장 (알림 없음)')
            continue
        counts = {'신규배정': 0, '작업변경': 0, '배정취소': 0, '작업취소': 0}
        for old, new in match_rows(old_rows, current):
            was, is_now = assigned(old), assigned(new)
            kind = None
            if not was and is_now:
                kind = '신규배정'
            elif was and new is None:
                kind = '작업취소'
            elif was and not is_now:
                kind = '배정취소'
            elif was and is_now and (any(old.get(k) != new.get(k) for k in CHANGE_FIELDS) or tug_changes(old, new)):
                kind = '작업변경'
            if kind and eligible(new or old, now):
                send(token, chat_id, kind, new or old, old, new)
                counts[kind] += 1
        print(f'{day}: 요청일시 기준 {len(current)}건 / 알림 {counts}')
    save_state(current_by_day, offset, keyboard_ready)
    if not keyboard_ready:
        telegram_send(token, chat_id, '🚢 서41 배정 조회 버튼이 준비됐습니다. 아래 버튼을 눌러 조회하세요.', keyboard=True)
        keyboard_ready = True
        save_state(current_by_day, offset, keyboard_ready)
    new_offset = process_commands(token, chat_id, current_by_day, now, offset)
    save_state(current_by_day, new_offset, keyboard_ready)


if __name__ == '__main__':
    main()
