#!/usr/bin/env python3
"""个人 IPTV TXT 白名单生成器（标准库实现，不下载/代理视频流）。"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
MAX_SOURCE_BYTES = 4 * 1024 * 1024
USER_AGENT = "PersonalIPTVPlaylist/1.0 (channel-list synchronization)"


def parse_txt(text: str) -> dict[str, list[str]]:
    """解析 #genre# 分组的 TXT；同名频道保留所有不同 URL 的顺序。"""
    channels: dict[str, list[str]] = {}
    for raw in text.lstrip('\ufeff').splitlines():
        row = raw.strip()
        if not row or row.startswith('#') or ',' not in row:
            continue
        name, url = row.split(',', 1)
        name, url = name.strip().lstrip('\ufeff'), url.strip()
        if not name or not url or url.lower() == '#genre#':
            continue
        channels.setdefault(name, [])
        if url not in channels[name]:
            channels[name].append(url)
    return channels


def valid_stream_url(url: str, *, hls_only: bool = True) -> bool:
    """仅做协议/形态过滤，不声称实际可播放。"""
    if any(x in url for x in ('\n', '\r', '\t', ' ')):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname
    except ValueError:
        return False
    if u.scheme.lower() not in ('http', 'https') or not host or u.username or u.password:
        return False
    if host.lower() == 'localhost' or host.lower().endswith(('.local', '.internal')):
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        pass  # public DNS host (DNS resolution is deliberately not performed)
    if hls_only and not u.path.lower().endswith('.m3u8'):
        return False
    return True


def unique(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(urls))


def fetch_source(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT, 'Accept': 'text/plain,*/*;q=0.5'
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        if getattr(resp, 'status', 200) != 200:
            raise ValueError(f'HTTP {resp.status}')
        content = resp.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError('source exceeds maximum allowed file size (4 MiB)')
    # UTF-8-BOM / UTF-8 first; refuse invalid encoding rather than corrupt names
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('source must be UTF-8') from exc


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def build(config: dict, upstream: dict[str, list[str]], previous: dict[str, list[str]],
          blocked: set[str], pinned: dict[str, list[str]]):
    """保留已存在且仍在上游的线路优先顺序；缺源时仅回退历史线路。"""
    hls_only = bool(config.get('hls_only', True))
    fallback = bool(config.get('keep_previous_if_missing', True))
    output: list[str] = []
    summary: list[dict] = []
    total = 0
    for group in config['groups']:
        group_rows: list[str] = []
        for ch in group['channels']:
            name = ch['name'].strip()
            aliases = unique([name] + ch.get('aliases', []))
            max_lines = int(ch.get('max_lines', config.get('max_lines_per_channel', 2)))
            if not 1 <= max_lines <= 10:
                raise ValueError(f'max_lines for {name} must be 1..10')
            found = unique([u for alias in aliases for u in upstream.get(alias, [])])
            old = unique([u for alias in aliases for u in previous.get(alias, [])])
            # pinned: preferred even if upstream changes; only use explicit user-provided URLs
            manually_selected = pinned.get(name, [])
            kept_old = [url for url in old if url in found]
            candidates = unique(manually_selected + kept_old + found)
            filtered = [u for u in candidates if u not in blocked and valid_stream_url(u, hls_only=hls_only)]
            result = filtered[:max_lines]
            state = 'current'
            if not result and fallback:
                result = [u for u in old if u not in blocked and valid_stream_url(u, hls_only=hls_only)][:max_lines]
                if result:
                    state = 'previous_unverified'
            if not result:
                state = 'missing'
            for url in result:
                group_rows.append(f'{name},{url}')
            total += len(result)
            summary.append({'channel': name, 'lines': len(result), 'state': state})
        if group_rows:
            output.append(f"{group['name']},#genre#")
            output.extend(group_rows)
            output.append('')
    if not total:
        raise ValueError('No channels matched or passed filtering; previous live.txt preserved')
    return '\n'.join(output).rstrip() + '\n', summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='从 TXT 上游筛选自己的影视仓直播源')
    parser.add_argument('--config', type=Path, default=ROOT / 'channels.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'live.txt')
    parser.add_argument('--blocked', type=Path, default=ROOT / 'blocked_urls.txt')
    parser.add_argument('--pinned', type=Path, default=ROOT / 'pinned.json')
    parser.add_argument('--source-file', type=Path, help='仅本地测试：改从本地文件读上游，完全不联网')
    args = parser.parse_args(argv)
    try:
        cfg = load_json(args.config)
        if not cfg.get('groups') or not cfg.get('sources'):
            raise ValueError('channels.json must contain non-empty sources and groups')
        names = [ch['name'] for g in cfg['groups'] for ch in g['channels']]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate channel name in whitelist')
        blocked = set()
        if args.blocked.exists():
            blocked = {s.strip() for s in args.blocked.read_text(encoding='utf-8-sig').splitlines()
                       if s.strip() and not s.lstrip().startswith('#')}
        pinned = load_json(args.pinned) if args.pinned.exists() else {}
        previous = parse_txt(args.output.read_text(encoding='utf-8-sig')) if args.output.exists() else {}
        upstream = {}
        if args.source_file is not None:
            texts = [(str(args.source_file), args.source_file.read_text(encoding='utf-8-sig'))]
        else:
            texts = []
            for source in cfg['sources']:
                # Explicitly specified source files only (never discover new aggregators)
                url = source['url']
                if not url.startswith('https://'):
                    raise ValueError('Playlist sources must use HTTPS')
                text = fetch_source(url)
                texts.append((url, text))
        for source_name, content in texts:
            parsed = parse_txt(content)
            print(f'Source {source_name}: {sum(map(len, parsed.values()))} lines, {len(parsed)} channel names')
            if len(parsed) < 1:
                raise ValueError('Upstream has no parseable channel rows')
            for ch, urls in parsed.items():
                upstream[ch] = unique(upstream.get(ch, []) + urls)
        output, summary = build(cfg, upstream, previous, blocked, pinned)
        missing = [r['channel'] for r in summary if r['state'] == 'missing']
        held = [r['channel'] for r in summary if r['state'] == 'previous_unverified']
        print(f'Selected {len(summary)-len(missing)} of {len(summary)} channels; '
              f'{sum(r["lines"] for r in summary)} stream candidate entries')
        print('Missing (no previous):', ', '.join(missing) if missing else 'none')
        print('Previous only (UNVERIFIED):', ', '.join(held) if held else 'none')
        payload = output.encode('utf-8')
        if args.output.exists() and args.output.read_bytes() == payload:
            print('NO_CHANGE: live.txt left untouched')
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(args.output.name + '.tmp')
        tmp.write_bytes(payload)
        os.replace(tmp, args.output)
        print(f'UPDATED: {args.output}')
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f'HOLD: {exc} (existing live.txt was not overwritten)', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
