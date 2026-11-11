#!/usr/bin/env python3
"""Deterministic public-source change monitor. Python standard library only."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import difflib
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
COMMITS_URL = 'https://api.github.com/repos/xai-org/x-algorithm/commits?per_page=1'
KEYWORDS = {
    'weights': ('weight', 'ranking', 'ranker', 'score', 'phoenix'),
    'visibility': ('visibility', 'filtering', 'label', 'moderation', 'suppression'),
    'rules_pricing': ('pricing', 'price', 'cost', 'rate card', 'billing', 'credit',
                      'automation', 'automated', 'bot', 'policy', 'policies', 'rules', 'compliance'),
}
NUMBER = r'[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?)(?:_?(?:f32|f64|[iu](?:8|16|32|64|128|size)))?'
CONSTANT = re.compile(r'\bconst\s+([A-Za-z_]\w*)\s*:\s*[^=;]+?=\s*(' + NUMBER + r')\s*;', re.MULTILINE)


def numeric_constants(text):
    """Parse Rust const declarations whose value is one numeric literal."""
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.DOTALL)
    result = {}
    for name, literal in CONSTANT.findall(text):
        suffix = r'_?[iu](?:8|16|32|64|128|size)$' if re.match(r'^[+-]?0[xXbBoO]', literal) else r'_?(?:f32|f64|[iu](?:8|16|32|64|128|size))$'
        value = re.sub(suffix, '', literal).replace('_', '')
        result[name] = Decimal(int(value, 0)) if re.match(r'^[+-]?0[xXbBoO]', value) else Decimal(value)
    return result


def numeric_diff(old, new):
    before, after = numeric_constants(old), numeric_constants(new)
    return [{'name': key, 'old': str(before[key]) if key in before else None,
             'new': str(after[key]) if key in after else None}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def classify(text):
    text = text.lower()
    return [category for category, words in KEYWORDS.items()
            if any(re.search(r'\b' + re.escape(word) + r'\b', text) for word in words)] or ['other']


def freshness(commits, now):
    latest = commits[0]
    date = datetime.fromisoformat(latest['commit']['committer']['date'].replace('Z', '+00:00'))
    if date.tzinfo is None or date > now:
        raise ValueError('Invalid commit timestamp')
    age = now - date
    return {'status': 'STALE' if age >= timedelta(days=14) else 'fresh',
            'categories': ['other'], 'commit': latest['sha'], 'last_commit_at': date.isoformat(),
            'age_days': round(age.total_seconds() / 86400, 6),
            'summary': 'STALE: no commit in 14+ days' if age >= timedelta(days=14) else 'upstream is fresh'}


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'public-assumption-watch', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8')


def local_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes root')
    return path


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def notify(summary, success, meaningful, offline=False):
    """Never put endpoint URLs or exception messages in public output."""
    warnings = []
    if offline:
        return warnings
    discord = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    health = os.environ.get('HEALTHCHECKS_PING_URL', '').strip()
    requests = []
    if discord and (meaningful or not success):
        requests.append(('Discord', discord, json.dumps({'content': summary[:1900]}).encode()))
    if health:
        try:
            parts = urllib.parse.urlsplit(health)
        except ValueError:
            warnings.append('Healthchecks notification failed')
            parts = None
        if parts is None:
            pass
        elif success:
            requests.append(('Healthchecks', health, None))
        elif parts.scheme == 'https' and (parts.hostname == 'hc-ping.com' or (parts.hostname or '').endswith('.hc-ping.com')):
            requests.append(('Healthchecks', urllib.parse.urlunsplit(parts._replace(path=parts.path.rstrip('/') + '/fail')), None))
    for label, url, data in requests:
        try:
            if urllib.parse.urlsplit(url).scheme != 'https':
                raise ValueError('HTTPS required')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'} if data else {})
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception:
            warnings.append(label + ' notification failed')
    return warnings


def run(config_path, output, fixture=None, now=None):
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    fixture = Path(fixture) if fixture else None
    report = {'run_at': now.isoformat(), 'offline': fixture is not None, 'targets': [], 'hard_failure': False}
    meaningful = []
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        for target in config['targets']:
            entry = {'id': target.get('id', 'invalid-target'), 'status': 'error', 'categories': [], 'diff_summary': ''}
            report['targets'].append(entry)
            if not target.get('enabled', True):
                entry.update(status='disabled', diff_summary=target.get('todo', 'disabled'))
                continue
            try:
                relative = target['repo'] + '/' + target['path']
                snapshot = local_path(output / 'snapshots', relative)
                url = 'https://raw.githubusercontent.com/' + target['repo'] + '/' + target.get('ref', 'main') + '/' + target['path']
                text = local_path(fixture, relative).read_text(encoding='utf-8') if fixture else fetch(url)
                if not snapshot.exists():
                    entry.update(status='baseline', diff_summary='baseline established')
                else:
                    old = snapshot.read_text(encoding='utf-8')
                    if old == text:
                        entry.update(status='unchanged', diff_summary='no changes')
                        continue
                    diff = ''.join(difflib.unified_diff(old.splitlines(True), text.splitlines(True), fromfile=relative + ' (old)', tofile=relative + ' (new)'))
                    entry.update(status='changed', diff=diff)
                    if target.get('kind') == 'numeric':
                        items = numeric_diff(old, text)
                        entry['numeric_changes'] = items
                        entry['categories'] = ['weights'] if items else ['other']
                        entry['diff_summary'] = '\n'.join(f"{item['name']}: {item['old']} -> {item['new']}" for item in items) or 'text changed; no numeric literal changes'
                    else:
                        changed_lines = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith(('+', '-')) and not line.startswith(('+++', '---')))
                        entry['categories'] = classify(changed_lines)
                        entry['diff_summary'] = 'text changed (' + ', '.join(entry['categories']) + ')'
                    # Whitespace-only edits update the snapshot but do not notify/log.
                    if re.sub(r'\s+', '', old) != re.sub(r'\s+', '', text):
                        meaningful.append(entry)
                atomic_write(snapshot, text)
            except Exception as error:
                entry.update(status='error', diff_summary='target failed (' + type(error).__name__ + ')')
                report['hard_failure'] = True
        try:
            commits = json.loads((fixture / 'commits.json').read_text(encoding='utf-8') if fixture else fetch(COMMITS_URL))
            report['freshness'] = freshness(commits, now)
            if report['freshness']['status'] == 'STALE':
                meaningful.append({'id': 'freshness', **report['freshness'], 'diff_summary': report['freshness']['summary']})
        except Exception as error:
            report['freshness'] = {'status': 'error', 'summary': 'freshness check failed (' + type(error).__name__ + ')'}
            report['hard_failure'] = True
        if meaningful:
            path = output / 'history' / (now.date().isoformat() + '.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n## ' + now.isoformat() + '\n\n')
                for entry in meaningful:
                    stream.write('### ' + entry['id'] + '\n\n' + entry['diff_summary'] + '\n\n')
                    if entry.get('diff'):
                        stream.write('```diff\n' + entry['diff'] + '\n```\n\n')
    except Exception as error:
        report['hard_failure'] = True
        report['error'] = 'run failed (' + type(error).__name__ + ')'
    lines = ['Watch: ' + ('FAILED' if report['hard_failure'] else 'OK')]
    lines.extend(entry['id'] + ': ' + entry['status'] + ' — ' + entry['diff_summary'] for entry in report['targets'])
    if 'freshness' in report:
        lines.append(report['freshness']['summary'])
    if 'error' in report:
        lines.append(report['error'])
    summary = '\n'.join(lines)
    report['notification_warnings'] = []
    try:
        atomic_write(output / 'last_run.json', json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    except Exception:
        report['hard_failure'] = True
        summary += '\nreport write failed'
    report['notification_warnings'] = notify(summary, not report['hard_failure'], bool(meaningful), fixture is not None)
    if report['notification_warnings']:
        try:
            atomic_write(output / 'last_run.json', json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        except Exception:
            pass
    print(summary)
    for warning in report['notification_warnings']:
        print(warning)
    return 1 if report['hard_failure'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-fixture', type=Path)
    parser.add_argument('--config', type=Path, default=ROOT / 'watch_targets.json')
    parser.add_argument('--output-dir', type=Path, default=ROOT)
    args = parser.parse_args()
    return run(args.config, args.output_dir, args.offline_fixture)


if __name__ == '__main__':
    raise SystemExit(main())
