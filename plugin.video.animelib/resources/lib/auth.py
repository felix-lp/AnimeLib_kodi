# -*- coding: utf-8 -*-
"""Login to AnimeLib (lib.social OAuth2 + PKCE, the same flow the site uses).

1. GET  auth.lib.social/auth/oauth/authorize?...   -> redirects to the login form
2. POST auth.lib.social/auth/login (login, password, CSRF token, ALTCHA proof-of-work)
3. follow redirects back to authorize -> <site>/ru/front/auth/oauth/callback?code=...
4. POST {api}/auth/oauth/token (authorization_code) -> access + refresh tokens
"""
import base64
import hashlib
import html
import json
import re
import secrets
import time
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

from . import http

AUTH_URL = 'https://auth.lib.social'
CLIENT_ID = '1'
MAX_REDIRECTS = 10


class AuthError(Exception):
    pass


def _pkce():
    verifier = secrets.token_urlsafe(96)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip('=')


def solve_altcha(challenge):
    """ALTCHA is a proof-of-work captcha: find n so that sha256(salt + n) == challenge."""
    algorithm = (challenge.get('algorithm') or 'SHA-256').replace('-', '').lower()
    salt = challenge['salt']
    target = challenge['challenge']
    maxnumber = int(challenge.get('maxnumber') or challenge.get('maxNumber') or 1000000)
    started = time.time()
    for number in range(maxnumber + 1):
        if hashlib.new(algorithm, (salt + str(number)).encode()).hexdigest() == target:
            payload = {
                'algorithm': challenge.get('algorithm', 'SHA-256'),
                'challenge': target,
                'number': number,
                'salt': salt,
                'signature': challenge.get('signature'),
                'took': int((time.time() - started) * 1000),
            }
            return base64.b64encode(json.dumps(payload).encode()).decode()
    raise AuthError('Не удалось решить капчу')


def _hidden_inputs(form_html):
    fields = {}
    for tag in re.findall(r'<input[^>]*>', form_html, re.S):
        name = re.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[name.group(1)] = html.unescape(value.group(1)) if value else ''
    return fields


def _page_error(page):
    for pattern in (r'class="[^"]*(?:form-error|invalid-feedback|alert|error)[^"]*"[^>]*>\s*([^<]{3,200})',):
        for msg in re.findall(pattern, page):
            msg = html.unescape(msg).strip()
            if msg:
                return msg
    return None


def login(username, password, site_url, api_url):
    """Returns a token dict (access_token, refresh_token, expires_in, timestamp)."""
    jar = CookieJar()
    opener = http.make_opener(jar, follow_redirects=False)
    redirect_uri = site_url.rstrip('/') + '/ru/front/auth/oauth/callback'
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(30)
    url = AUTH_URL + '/auth/oauth/authorize?' + urlencode({
        'scope': '', 'client_id': CLIENT_ID, 'response_type': 'code',
        'redirect_uri': redirect_uri, 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })

    def follow(resp):
        """Follow redirects until we reach the callback (returns code) or a page."""
        for _ in range(MAX_REDIRECTS):
            location = resp.headers.get('Location') if resp.code in (301, 302, 303, 307, 308) else None
            if not location:
                return None, resp
            location = urljoin(resp.url, location)
            if location.startswith(redirect_uri):
                query = parse_qs(urlparse(location).query)
                if 'error' in query:
                    raise AuthError(query.get('error_description', query['error'])[0])
                if query.get('state', [None])[0] != state:
                    raise AuthError('OAuth state mismatch')
                return query['code'][0], None
            resp = http.fetch(location, opener=opener)
        raise AuthError('Too many redirects')

    code, page = follow(http.fetch(url, opener=opener))
    if code is None:
        if page.code >= 400:
            raise AuthError('auth.lib.social: HTTP %d' % page.code)
        form = re.search(r'<form[^>]*action="([^"]*auth/login[^"]*)"[^>]*>(.*?)</form>', page.text, re.S)
        if not form:
            raise AuthError('Форма входа не найдена')
        fields = _hidden_inputs(form.group(2))
        fields.pop('login', None)
        fields.pop('password', None)
        fields.update({'login': username, 'password': password})
        if 'altcha' in page.text:
            ch = http.fetch(AUTH_URL + '/altcha/challenge', opener=opener,
                            headers={'Accept': 'application/json'})
            if ch.code >= 400:
                raise AuthError('Не удалось получить капчу (HTTP %d)' % ch.code)
            fields['altcha'] = solve_altcha(ch.json())
        action = html.unescape(form.group(1))
        resp = http.fetch(urljoin(page.url, action), data=fields, opener=opener, retries=0,
                          headers={'Referer': page.url, 'Origin': AUTH_URL})
        code, page = follow(resp)
        if code is None:
            # An authorization approval form may be shown for the client.
            approve = re.search(r'<form[^>]*action="([^"]*oauth/authorize[^"]*)"[^>]*>(.*?)</form>',
                                page.text, re.S)
            if approve and 'auth/login' not in page.url:
                resp = http.fetch(urljoin(page.url, html.unescape(approve.group(1))),
                                  data=_hidden_inputs(approve.group(2)), opener=opener,
                                  headers={'Referer': page.url, 'Origin': AUTH_URL})
                code, page = follow(resp)
        if code is None:
            raise AuthError(_page_error(page.text) or 'Не удалось войти')

    resp = http.fetch(api_url.rstrip('/') + '/auth/oauth/token', json_body={
        'grant_type': 'authorization_code', 'client_id': CLIENT_ID,
        'redirect_uri': redirect_uri, 'code_verifier': verifier, 'code': code,
    }, headers=_api_headers(site_url))
    return _token_from(resp)


def refresh(token, site_url, api_url):
    resp = http.fetch(api_url.rstrip('/') + '/auth/oauth/token', json_body={
        'grant_type': 'refresh_token', 'client_id': CLIENT_ID,
        'refresh_token': token['refresh_token'], 'scope': '',
    }, headers=_api_headers(site_url))
    return _token_from(resp)


def _api_headers(site_url):
    site = site_url.rstrip('/')
    return {'Accept': 'application/json', 'Site-Id': '5', 'Referer': site + '/', 'Origin': site}


def _token_from(resp):
    if resp.code >= 400:
        try:
            data = resp.json()
            msg = data.get('message') or data.get('error_description') or data.get('error')
        except http.HttpError:
            msg = None
        raise AuthError(msg or 'HTTP %d' % resp.code)
    data = resp.json()
    data = data.get('data', data) if isinstance(data, dict) else data
    if not data.get('access_token'):
        raise AuthError('Сервер не вернул токен')
    data['timestamp'] = time.time()
    return data


def is_expired(token, margin=300):
    return time.time() >= token.get('timestamp', 0) + int(token.get('expires_in') or 0) - margin
