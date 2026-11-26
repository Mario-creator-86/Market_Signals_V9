(function () {
    'use strict';

    const CSRF_STORAGE_KEY = 'market-signals.operator-csrf.v1';
    const AUTH_NOTICE_KEY = 'market-signals.operator-auth-notice.v1';
    const nativeFetch = window.fetch.bind(window);
    let authEnabled = false;
    let authenticated = false;
    let modal = null;
    let authReadyResolved = false;
    let resolveAuthReady;
    window.MSAuthReady = new Promise(resolve => {
        resolveAuthReady = resolve;
    });

    function releaseApplication() {
        if (authReadyResolved) return;
        authReadyResolved = true;
        resolveAuthReady();
    }

    function csrfToken() {
        try {
            return window.sessionStorage.getItem(CSRF_STORAGE_KEY) || '';
        } catch (_) {
            return '';
        }
    }

    function setCsrfToken(value) {
        try {
            if (value) window.sessionStorage.setItem(CSRF_STORAGE_KEY, String(value));
            else window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
        } catch (_) {
            // Storage can be unavailable in hardened/private browser contexts.
        }
    }

    function setAuthNotice(value) {
        try {
            if (value) window.sessionStorage.setItem(AUTH_NOTICE_KEY, String(value));
            else window.sessionStorage.removeItem(AUTH_NOTICE_KEY);
        } catch (_) {
            // A missing notice does not affect the authentication boundary.
        }
    }

    function takeAuthNotice() {
        try {
            const value = window.sessionStorage.getItem(AUTH_NOTICE_KEY) || '';
            window.sessionStorage.removeItem(AUTH_NOTICE_KEY);
            return value;
        } catch (_) {
            return '';
        }
    }

    function apiUrl(input) {
        try {
            const raw = typeof input === 'string' ? input : input?.url;
            const parsed = new URL(raw, window.location.href);
            return parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/')
                ? parsed
                : null;
        } catch (_) {
            return null;
        }
    }

    window.fetch = async function authenticatedFetch(input, init) {
        const options = { ...(init || {}) };
        const target = apiUrl(input);
        if (target) {
            const method = String(
                options.method || (typeof input !== 'string' && input?.method) || 'GET'
            ).toUpperCase();
            const headers = new Headers(
                options.headers || (typeof input !== 'string' ? input?.headers : undefined)
            );
            if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
                const csrf = csrfToken();
                if (csrf && !headers.has('X-CSRF-Token')) {
                    headers.set('X-CSRF-Token', csrf);
                }
            }
            options.headers = headers;
            options.credentials = options.credentials || 'same-origin';
        }

        const response = await nativeFetch(input, options);
        if (
            target
            && response.status === 401
            && !target.pathname.startsWith('/api/auth/')
        ) {
            const hadSession = authenticated;
            authenticated = false;
            setCsrfToken('');
            if (hadSession) {
                setAuthNotice('Sesiunea a expirat. Autentifică-te din nou.');
                window.location.reload();
            } else {
                showLogin('Autentifică-te pentru a continua.');
            }
        }
        return response;
    };

    function ensureModal() {
        if (modal) return modal;
        const wrapper = document.createElement('div');
        wrapper.id = 'operator-auth-modal';
        wrapper.className = 'operator-auth hidden';
        wrapper.setAttribute('role', 'dialog');
        wrapper.setAttribute('aria-modal', 'true');
        wrapper.setAttribute('aria-labelledby', 'operator-auth-title');
        wrapper.innerHTML = `
            <form class="operator-auth__card" id="operator-auth-form">
                <div class="operator-auth__mark"><i class="fa-solid fa-shield-halved"></i></div>
                <p class="operator-auth__eyebrow">SECURE OPERATOR ACCESS</p>
                <h2 id="operator-auth-title">Market Signals Control Room</h2>
                <p class="operator-auth__copy">Introdu cheia API de producție. Cheia este folosită o singură dată și nu este salvată în browser.</p>
                <label class="operator-auth__field">
                    <span>API key</span>
                    <input id="operator-auth-key" type="password" autocomplete="current-password"
                        minlength="1" required spellcheck="false" aria-describedby="operator-auth-message">
                </label>
                <p id="operator-auth-message" class="operator-auth__message" aria-live="polite"></p>
                <button id="operator-auth-submit" type="submit">
                    <i class="fa-solid fa-lock"></i><span>Deschide sesiunea securizată</span>
                </button>
                <p class="operator-auth__note"><i class="fa-solid fa-circle-info"></i> Sesiune HttpOnly, SameSite=Strict, protejată CSRF.</p>
            </form>
        `;
        document.body.appendChild(wrapper);
        wrapper.querySelector('#operator-auth-form').addEventListener('submit', login);
        modal = wrapper;
        return wrapper;
    }

    function showLogin(message) {
        if (!authEnabled && !message) return;
        const dialog = ensureModal();
        const output = dialog.querySelector('#operator-auth-message');
        output.textContent = message || '';
        output.classList.toggle('is-error', Boolean(message));
        dialog.classList.remove('hidden');
        window.setTimeout(() => dialog.querySelector('#operator-auth-key')?.focus(), 0);
    }

    function hideLogin() {
        if (modal) modal.classList.add('hidden');
    }

    async function login(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const input = form.querySelector('#operator-auth-key');
        const submit = form.querySelector('#operator-auth-submit');
        const message = form.querySelector('#operator-auth-message');
        const apiKey = String(input.value || '');
        submit.disabled = true;
        message.classList.remove('is-error');
        message.textContent = 'Se verifică accesul…';
        try {
            const response = await nativeFetch('/api/auth/login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_key: apiKey })
            });
            input.value = '';
            if (!response.ok) {
                throw new Error(response.status === 429
                    ? 'Prea multe încercări. Reîncearcă peste un minut.'
                    : 'Cheie API invalidă.');
            }
            const payload = await response.json();
            setCsrfToken(payload.csrf_token);
            authenticated = true;
            hideLogin();
            window.location.reload();
        } catch (error) {
            message.textContent = error?.message || 'Autentificarea nu a reușit.';
            message.classList.add('is-error');
        } finally {
            submit.disabled = false;
        }
    }

    function installSessionButton() {
        if (!authEnabled || !authenticated || document.getElementById('operator-session-button')) return;
        const header = document.querySelector('.app-header .header-content');
        if (!header) return;
        const button = document.createElement('button');
        button.id = 'operator-session-button';
        button.className = 'operator-session-button';
        button.type = 'button';
        button.title = 'Sesiune securizată activă · click pentru logout';
        button.setAttribute('aria-label', 'Închide sesiunea securizată');
        button.innerHTML = '<i class="fa-solid fa-shield-check"></i><span>Secure</span>';
        button.addEventListener('click', async () => {
            try {
                await window.fetch('/api/auth/logout', { method: 'POST' });
            } finally {
                setCsrfToken('');
                authenticated = false;
                setAuthNotice('Sesiunea a fost închisă.');
                window.location.reload();
            }
        });
        header.appendChild(button);
    }

    async function bootstrap() {
        try {
            const response = await nativeFetch('/api/auth/status', {
                credentials: 'same-origin',
                cache: 'no-store'
            });
            if (!response.ok) return;
            const status = await response.json();
            authEnabled = Boolean(status.auth_enabled);
            authenticated = Boolean(status.authenticated);
            if (status.csrf_token) setCsrfToken(status.csrf_token);
            if (authEnabled && !authenticated) {
                setCsrfToken('');
                showLogin(takeAuthNotice());
            } else if (authEnabled) {
                installSessionButton();
                releaseApplication();
            } else {
                releaseApplication();
            }
        } catch (_) {
            // The existing offline banner handles backend connectivity failures.
            releaseApplication();
        }
    }

    window.MSAuth = {
        bootstrap,
        showLogin,
        isAuthenticated: () => authenticated,
        isEnabled: () => authEnabled
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
    } else {
        bootstrap();
    }
})();
