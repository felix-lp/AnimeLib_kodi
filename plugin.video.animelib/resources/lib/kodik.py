# -*- coding: utf-8 -*-
"""Resolver for Kodik player links (kodikplayer.com/seria/... and similar)."""
import base64
import json
import re
from urllib.parse import unquote, urljoin

from . import http

DEFAULT_ENDPOINT = '/ftor'
DEFAULT_SHIFT = 18


class KodikError(Exception):
    pass


def _shift_letters(s, shift):
    out = []
    for c in s:
        if 'a' <= c <= 'z':
            out.append(chr((ord(c) - 97 + shift) % 26 + 97))
        elif 'A' <= c <= 'Z':
            out.append(chr((ord(c) - 65 + shift) % 26 + 65))
        else:
            out.append(c)
    return ''.join(out)


def _b64(s):
    s = s.replace('-', '+').replace('_', '/')
    return base64.b64decode(s + '=' * (-len(s) % 4)).decode('utf-8')


def decode_src(src, shift=DEFAULT_SHIFT):
    """Kodik obfuscates links as base64 with a Caesar shift over the letters.
    The shift changes from time to time, so fall back to trying all of them."""
    if '//' in src:
        return src
    for k in [shift] + [i for i in range(26) if i != shift]:
        try:
            url = _b64(_shift_letters(src, k))
        except Exception:
            continue
        if '//' in url and ('.m3u8' in url or '.mp4' in url or url.startswith(('http', '//'))):
            return url
    raise KodikError('Unable to decode Kodik link')


def _js_var(html, name):
    m = re.search(r'''\bvar\s+%s\s*=\s*["']([^"']*)["']''' % re.escape(name), html)
    return m.group(1) if m else None


def _find_endpoint_and_shift(page_url, html, headers):
    endpoint, shift = DEFAULT_ENDPOINT, DEFAULT_SHIFT
    m = re.search(r'src="(/assets/js/app\.player[^"]+\.js)"', html)
    if not m:
        return endpoint, shift
    try:
        js = http.request(urljoin(page_url, m.group(1)), headers=headers)
    except http.HttpError:
        return endpoint, shift
    m = re.search(r'type:\s*"POST",\s*url:\s*atob\("([^"]+)"\)', js)
    if m:
        try:
            endpoint = _b64(m.group(1))
        except Exception:
            pass
    m = re.search(r'charCodeAt\(0\)\s*\+\s*(\d+)\)', js)
    if m:
        shift = int(m.group(1)) % 26
    return endpoint, shift


def resolve(player_url, referer='https://anilib.me/'):
    """Returns dict {quality(int): url}."""
    if player_url.startswith('//'):
        player_url = 'https:' + player_url
    page_headers = {'Referer': referer}
    html = http.request(player_url, headers=page_headers)

    params = {}
    m = re.search(r"urlParams\s*=\s*'(\{.*?\})'", html)
    if m:
        try:
            params = json.loads(m.group(1))
        except ValueError:
            params = {}
    post = {}
    for key, var in (('d', 'domain'), ('d_sign', 'd_sign'), ('pd', 'pd'),
                     ('pd_sign', 'pd_sign'), ('ref', 'ref'), ('ref_sign', 'ref_sign')):
        value = _js_var(html, var)
        if value is None:
            value = params.get(key)
            if key == 'ref' and value:
                value = unquote(value)
        post[key] = value or ''
    post['bad_user'] = 'false'
    post['cdn_is_working'] = 'true'

    for key in ('type', 'hash', 'id'):
        m = re.search(r"vInfo\.%s\s*=\s*'([^']*)'" % key, html)
        if not m:
            raise KodikError('Kodik page format changed (vInfo.%s not found)' % key)
        post[key] = m.group(1)

    endpoint, shift = _find_endpoint_and_shift(player_url, html, page_headers)
    origin = re.match(r'(https?://[^/]+)', player_url).group(1)
    resp = http.get_json(urljoin(origin, endpoint), data=post, headers={
        'Referer': player_url,
        'Origin': origin,
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
    })

    links = resp.get('links') or {}
    result = {}
    for quality, variants in links.items():
        if not variants:
            continue
        url = decode_src(variants[0].get('src', ''), shift)
        if url.startswith('//'):
            url = 'https:' + url
        try:
            result[int(quality)] = url
        except ValueError:
            continue
    if not result:
        raise KodikError('Kodik returned no video links')
    return result
