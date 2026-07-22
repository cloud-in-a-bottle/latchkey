import html
import json
from typing import Any

from litestar import MediaType
from litestar import Request
from litestar import get

from server.grants import parse_grant_payload

_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a2e; }
  h1 { font-size: 1.4rem; }  h2 { font-size: 1.1rem; margin-top: 2rem; }
  table { border-collapse: collapse; width: 100%; }
  td, th { text-align: left; padding: 0.4rem 0.8rem; border-bottom: 1px solid #e0e0e8; }
  button { padding: 0.35rem 0.9rem; border: 1px solid #4a4ae0; background: #fff; color: #4a4ae0;
           border-radius: 6px; cursor: pointer; }
  button.primary { background: #4a4ae0; color: #fff; }
  button:disabled { opacity: 0.5; cursor: default; }
  input, textarea { width: 100%; box-sizing: border-box; padding: 0.4rem; margin: 0.3rem 0;
                    border: 1px solid #c0c0d0; border-radius: 6px; font-family: ui-monospace, monospace; }
  .muted { color: #777; font-size: 0.9rem; }
  .status-valid { color: #0a7d36; }  .status-invalid, .status-missing { color: #b3261e; }
  .card { border: 1px solid #e0e0e8; border-radius: 10px; padding: 1rem 1.4rem; margin: 1rem 0; }
  iframe { border: 1px solid #c0c0d0; border-radius: 8px; width: 100%; height: 640px; }
  code { background: #f2f2f7; padding: 0.1rem 0.3rem; border-radius: 4px; }
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}<script>{script}</script></body></html>"
    )


@get("/", media_type=MediaType.HTML)
async def console() -> str:
    body = """
    <h1>Latchkey</h1>
    <p class="muted">Connect third-party services here; other apps call them through latchkey
    without ever seeing the credentials.</p>
    <div id="gateway-warning"></div>
    <h2>Connected services</h2>
    <table id="connected"><tbody></tbody></table>
    <h2>Available services</h2>
    <p class="muted">Connect with a browser login where supported, or paste credentials manually.</p>
    <table id="available"><tbody></tbody></table>
    <div class="card">
      <h2 style="margin-top:0">Register a custom service</h2>
      <p class="muted">For self-hosted or unlisted services (credentials must then be set manually).</p>
      <input id="reg-name" placeholder="service name (e.g. my-gitlab)">
      <input id="reg-url" placeholder="base API URL (e.g. https://gitlab.example.com/api/v4/)">
      <input id="reg-family" placeholder="service family (optional, e.g. gitlab)">
      <button class="primary" onclick="registerService()">Register</button>
      <span id="reg-result" class="muted"></span>
    </div>
    """
    script = """
    async function refresh() {
      const r = await fetch('/owner/api/status');
      const s = await r.json();
      if (!s.gateway_healthy) {
        document.getElementById('gateway-warning').innerHTML =
          '<div class="card" style="border-color:#b3261e">latchkey gateway is not running — check app logs</div>';
        return;
      }
      // s.auth: {serviceName: {credentialType, credentialStatus}}
      const connected = document.querySelector('#connected tbody');
      connected.innerHTML = '';
      const authed = Object.entries(s.auth || {});
      if (authed.length === 0) connected.innerHTML = '<tr><td class="muted">none yet</td></tr>';
      for (const [name, info] of authed) {
        const status = info.credentialStatus || '';
        connected.innerHTML +=
          `<tr><td><a href="/connect/${name}">${name}</a></td><td class="status-${status}">${status}</td>` +
          `<td><button onclick="clearAuth('${name}')">Disconnect</button></td></tr>`;
      }
      // s.services: array of service name strings
      const available = document.querySelector('#available tbody');
      available.innerHTML = '';
      for (const name of (s.services || [])) {
        available.innerHTML +=
          `<tr><td>${name}</td>` +
          `<td><a href="/connect/${name}"><button class="primary">Connect</button></a></td></tr>`;
      }
    }
    async function clearAuth(name) {
      await fetch('/owner/api/auth/clear', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({service_name: name})});
      refresh();
    }
    async function registerService() {
      const out = document.getElementById('reg-result');
      out.textContent = '...';
      const payload = {service_name: document.getElementById('reg-name').value,
                       base_api_url: document.getElementById('reg-url').value};
      const family = document.getElementById('reg-family').value;
      if (family) payload.service_family = family;
      const r = await fetch('/owner/api/services/register', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const body = await r.json();
      out.textContent = r.ok ? 'registered' : (body.message || 'failed');
      refresh();
    }
    refresh();
    """
    return _page("Latchkey", body, script)


@get("/connect/{service_name:str}", media_type=MediaType.HTML)
async def connect_page(service_name: str) -> str:
    safe_name = html.escape(service_name)
    body = f"""
    <h1>Connect {safe_name}</h1>
    <p id="cred-status" class="muted"></p>
    <div class="card" id="browser-card" style="display:none">
      <h2 style="margin-top:0">Browser login</h2>
      <p class="muted">A browser opens below. Log in to {safe_name} as usual — latchkey watches the
      session, extracts API credentials once you're logged in, and closes the browser. Credentials are
      stored encrypted inside this app and are never shown to other apps.</p>
      <p><button class="primary" id="start" onclick="start()">Start login</button>
         <span id="state" class="muted"></span></p>
      <iframe id="vnc" style="display:none"
        src="/novnc/vnc.html?autoconnect=1&resize=scale&path=owner/vnc"></iframe>
    </div>
    <div class="card">
      <h2 style="margin-top:0">Manual credentials</h2>
      <p class="muted" id="set-example">Stored as curl arguments.</p>
      <textarea id="curl-args" rows="2" placeholder='-H "Authorization: Bearer ..."'></textarea>
      <button class="primary" onclick="setCredentials()">Save credentials</button>
      <span id="set-result" class="muted"></span>
    </div>
    <p><a href="/">&larr; back to console</a></p>
    """
    script = f"""
    const serviceName = {json.dumps(service_name)};
    let polling = null;
    async function init() {{
      const r = await fetch(`/owner/api/service-info/${{encodeURIComponent(serviceName)}}`);
      if (!r.ok) {{
        document.getElementById('cred-status').textContent = 'unknown service';
        return;
      }}
      const info = await r.json();
      document.getElementById('cred-status').textContent =
        `credentials: ${{info.credentialStatus || 'unknown'}}`;
      if ((info.authOptions || []).includes('browser')) {{
        document.getElementById('browser-card').style.display = 'block';
      }}
      if (info.setCredentialsExample) {{
        document.getElementById('set-example').innerHTML =
          'Stored as curl arguments, e.g. <code></code>';
        document.querySelector('#set-example code').textContent =
          info.setCredentialsExample.replace(/^latchkey auth set \\S+ /, '');
      }}
    }}
    async function start() {{
      document.getElementById('start').disabled = true;
      document.getElementById('vnc').style.display = 'block';
      const r = await fetch('/owner/api/browser-login/start', {{method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{service_name: serviceName}})}});
      if (!r.ok) {{
        const body = await r.json();
        document.getElementById('state').textContent = body.message || 'failed to start';
        document.getElementById('start').disabled = false;
        return;
      }}
      document.getElementById('state').textContent = 'waiting for login…';
      polling = setInterval(poll, 2000);
    }}
    async function poll() {{
      const r = await fetch('/owner/api/browser-login/status');
      const s = await r.json();
      if (s.state === 'succeeded') {{
        clearInterval(polling);
        document.getElementById('state').textContent = 'connected!';
        document.getElementById('vnc').style.display = 'none';
        init();
      }} else if (s.state === 'failed') {{
        clearInterval(polling);
        document.getElementById('state').textContent = 'failed: ' + (s.error || 'unknown error');
        document.getElementById('start').disabled = false;
      }}
    }}
    async function setCredentials() {{
      const out = document.getElementById('set-result');
      out.textContent = '...';
      const r = await fetch('/owner/api/auth/set', {{method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{service_name: serviceName,
                              curl_args: document.getElementById('curl-args').value}})}});
      const body = await r.json();
      out.textContent = r.ok ? 'saved' : (body.message || 'failed');
      init();
    }}
    init();
    """
    return _page(f"Connect {service_name}", body, script)


@get("/grant", media_type=MediaType.HTML)
async def grant_page(request: Request[Any, Any, Any]) -> str:
    params = request.query_params
    consumer_id = str(params.get("consumer_id", ""))
    consumer_name = str(params.get("consumer_name", ""))
    return_to = str(params.get("return_to", ""))
    grant = parse_grant_payload(_parse_json_param(params.get("grant")))

    if consumer_id == "" or consumer_name == "":
        return _page("Grant access", "<h1>Grant access</h1><p>Missing consumer info in the link.</p>")
    if grant is None:
        body = f"""
        <h1>Grant access</h1>
        <p><strong>{html.escape(consumer_name)}</strong> wants to use latchkey, but didn't specify a
        valid grant (which API scope and permissions it needs). Ask the app's author.</p>
        <p><a href="/">&larr; console</a></p>
        """
        return _page("Grant access", body)

    permission_items = "".join(f"<li><code>{html.escape(p)}</code></li>" for p in grant.permissions)
    schemas_note = ""
    if grant.schemas:
        schema_payload = grant.as_payload().get("schemas")
        schemas_note = (
            "<p>Custom request-matching schemas (for services latchkey doesn't know built-in):</p>"
            f"<pre><code>{html.escape(json.dumps(schema_payload, indent=2))}</code></pre>"
        )
    body = f"""
    <h1>Grant access?</h1>
    <div class="card">
      <p>The app <strong>{html.escape(consumer_name)}</strong> is asking to call third-party APIs
      through latchkey using <em>your</em> stored credentials:</p>
      <p>API scope: <code>{html.escape(grant.scope)}</code></p>
      <p>Allowed actions:</p><ul>{permission_items}</ul>
      {schemas_note}
      <p class="muted">The app will never see the credentials themselves; latchkey injects them and
      only allows requests matching these permissions.</p>
      <p>
        <button class="primary" onclick="approve()">Allow</button>
        <button onclick="deny()">Deny</button>
        <span id="result" class="muted"></span>
      </p>
    </div>
    """
    script = f"""
    const returnTo = {json.dumps(return_to)};
    async function approve() {{
      const out = document.getElementById('result');
      out.textContent = '...';
      const r = await fetch('/owner/api/grants/approve', {{method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{consumer_app_id: {json.dumps(consumer_id)},
                              grant: {json.dumps(grant.as_payload())}}})}});
      if (!r.ok) {{
        const body = await r.json();
        out.textContent = body.message || 'failed';
        return;
      }}
      out.textContent = 'granted';
      if (returnTo) window.location = returnTo;
    }}
    function deny() {{
      if (returnTo) window.location = returnTo; else window.location = '/';
    }}
    """
    return _page("Grant access", body, script)


def _parse_json_param(value: object) -> object:
    if not isinstance(value, str) or value == "":
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
