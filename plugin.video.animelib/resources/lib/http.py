# -*- coding: utf-8 -*-
"""Minimal HTTP helpers built on urllib (no external dependencies)."""
import gzip
import json
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
TIMEOUT = 20


class HttpError(Exception):
    pass


def _decode_body(resp):
    data = resp.read()
    encoding = (resp.headers.get('Content-Encoding') or '').lower()
    if encoding == 'gzip':
        data = gzip.decompress(data)
    elif encoding == 'deflate':
        data = zlib.decompress(data, -zlib.MAX_WBITS)
    charset = resp.headers.get_content_charset() or 'utf-8'
    return data.decode(charset, errors='replace')


def request(url, params=None, data=None, headers=None):
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params, doseq=True)
    hdrs = {
        'User-Agent': USER_AGENT,
        'Accept-Encoding': 'gzip, deflate',
    }
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = urlencode(data).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8')
    try:
        with urlopen(Request(url, data=body, headers=hdrs), timeout=TIMEOUT) as resp:
            return _decode_body(resp)
    except HTTPError as e:
        raise HttpError('HTTP %d: %s' % (e.code, url))
    except URLError as e:
        raise HttpError('%s: %s' % (e.reason, url))


def get_json(url, params=None, data=None, headers=None):
    text = request(url, params=params, data=data, headers=headers)
    try:
        return json.loads(text)
    except ValueError:
        raise HttpError('Invalid JSON from %s' % url)
