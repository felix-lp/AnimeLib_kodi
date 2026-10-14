# -*- coding: utf-8 -*-
"""Minimal HTTP helpers built on urllib (no external dependencies)."""
import gzip
import json
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
TIMEOUT = 20
RETRY_CODES = (502, 503, 504)


class HttpError(Exception):
    def __init__(self, message, code=None, body=None):
        Exception.__init__(self, message)
        self.code = code
        self.body = body


class Response(object):
    def __init__(self, code, headers, text, url):
        self.code = code
        self.headers = headers
        self.text = text
        self.url = url

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            raise HttpError('Invalid JSON from %s' % self.url, self.code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _decode_body(resp):
    data = resp.read()
    encoding = (resp.headers.get('Content-Encoding') or '').lower()
    if encoding == 'gzip':
        data = gzip.decompress(data)
    elif encoding == 'deflate':
        data = zlib.decompress(data, -zlib.MAX_WBITS)
    charset = resp.headers.get_content_charset() or 'utf-8'
    return data.decode(charset, errors='replace')


def make_opener(cookiejar=None, follow_redirects=True):
    handlers = []
    if cookiejar is not None:
        handlers.append(HTTPCookieProcessor(cookiejar))
    if not follow_redirects:
        handlers.append(_NoRedirect())
    return build_opener(*handlers)


_default_opener = make_opener()


def fetch(url, params=None, data=None, json_body=None, headers=None, method=None,
          opener=None, retries=2):
    """Performs a request and returns Response for any HTTP status (raises only on network errors)."""
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params, doseq=True)
    hdrs = {
        'User-Agent': USER_AGENT,
        'Accept-Encoding': 'gzip, deflate',
    }
    if headers:
        hdrs.update(headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode('utf-8')
        hdrs['Content-Type'] = 'application/json'
    elif data is not None:
        body = urlencode(data).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8')
    opener = opener or _default_opener
    attempt = 0
    while True:
        req = Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                result = Response(resp.status, resp.headers, _decode_body(resp), url)
        except HTTPError as e:
            try:
                text = _decode_body(e)
            except Exception:
                text = ''
            result = Response(e.code, e.headers, text, url)
        except URLError as e:
            raise HttpError('%s: %s' % (e.reason, url))
        if result.code in RETRY_CODES and attempt < retries:
            attempt += 1
            time.sleep(attempt)
            continue
        return result


def request(url, params=None, data=None, headers=None, **kwargs):
    resp = fetch(url, params=params, data=data, headers=headers, **kwargs)
    if resp.code >= 400:
        raise HttpError('HTTP %d: %s' % (resp.code, url), resp.code, resp.text)
    return resp.text


def get_json(url, params=None, data=None, headers=None, **kwargs):
    resp = fetch(url, params=params, data=data, headers=headers, **kwargs)
    if resp.code >= 400:
        raise HttpError('HTTP %d: %s' % (resp.code, url), resp.code, resp.text)
    return resp.json()
