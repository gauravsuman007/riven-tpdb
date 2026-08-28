r"""The page the Jellyfin WebView shells load, and why it is shaped like this.

Two of the clients people actually own -- the official **Jellyfin for Android**
phone app and the **LG webOS** app -- are not native clients. They are WebView
shells: they validate the server over the API, then load their entire interface
from the server and render it. They ship no UI of their own. Against an
API-only masquerade they show a spinner and then "connection cannot be
established", which is what sent someone debugging their network for an hour.

Reading `jellyfin-android` (GPL, so this is reading source, not guesswork) the
contract turns out to be far weaker than "serve the real jellyfin-web":

1. **Connected** is decided in `JellyfinWebViewClient.shouldInterceptRequest`:

       path.matches(Constants.MAIN_BUNDLE_PATH_REGEX) && "deferred" !in query
           -> onConnectedToWebapp()

       MAIN_BUNDLE_PATH_REGEX = Regex( .*/main\.[^/\s]+\.bundle\.js )

   It matches on the REQUEST PATH and never looks at the response. So any page
   that merely *requests* `.../main.<something>.bundle.js` satisfies the shell.
   Verified live against the real app before this module was written.
   `INITIAL_CONNECTION_TIMEOUT` is 10s -- miss it and the client errors.

   That is why the whole app is served AS the bundle: one request both trips
   the connected flag and delivers the code. Renaming this path breaks the
   client silently, with a spinner and no log line. Do not rename it.

2. **Credentials** are read straight out of our localStorage:

       JSON.parse(window.localStorage.getItem('jellyfin_credentials'))
       -> Servers[0].UserId / Servers[0].AccessToken

   So logging in here is what hands the native layer its API token. Our token
   is simply the Riven API key (see `auth.py`), which is why the login form
   asks for it as the password rather than inventing a second credential.

3. **Playback** goes through `window.NativePlayer`:

       NativePlayer.isEnabled()          -- true when ExoPlayer is selected
       NativePlayer.loadPlayer(json)     -- {"ids":[...], "startIndex":0, ...}

   `PlayOptions.fromJson` takes ITEM IDS, not URLs: ExoPlayer then resolves the
   stream itself against `/Items/{id}/PlaybackInfo` and `/Videos/{id}/stream`,
   both of which this server already implements. Nothing extra was needed on
   the backend to make native playback work.

Kept deliberately dependency-free and small: no build step, no framework, and
it must stay usable with a TV remote (hence large tiles and arrow-key focus),
because the same page is what a webOS TV will load.
"""

from program.settings import settings_manager

#: The path that trips `onConnectedToWebapp()`. Must match
#: `.*/main\.[^/\s]+\.bundle\.js` -- see the module docstring.
BUNDLE_PATH = "/web/main.riven.bundle.js"

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Riven</title>
<script src="__BUNDLE__" defer></script>
</head>
<body><div id="app"></div></body>
</html>
"""

# NOTE: plain ES5-ish JS on purpose. webOS ships an old Chromium and the
# Android WebView minimum this client supports is 80.
BUNDLE_JS = r"""
(function () {
  "use strict";

  var USERNAME = "__USERNAME__";
  var CRED_KEY = "jellyfin_credentials";
  var app = document.getElementById("app");

  // ---- credentials -------------------------------------------------------
  // This exact shape is what jellyfin-android parses out of localStorage.
  function readCreds() {
    try {
      var c = JSON.parse(window.localStorage.getItem(CRED_KEY));
      if (c && c.Servers && c.Servers[0] && c.Servers[0].AccessToken) return c.Servers[0];
    } catch (e) {}
    return null;
  }

  function writeCreds(serverId, userId, token) {
    window.localStorage.setItem(CRED_KEY, JSON.stringify({
      Servers: [{ Id: serverId, UserId: userId, AccessToken: token }]
    }));
  }

  function token() { var c = readCreds(); return c ? c.AccessToken : null; }
  function userId() { var c = readCreds(); return c ? c.UserId : null; }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    var t = token();
    if (t) opts.headers["X-Emby-Token"] = t;
    opts.headers["Authorization"] =
      'MediaBrowser Client="Riven Web", Device="Riven", DeviceId="riven-webapp", Version="1.0"';
    return fetch(path, opts);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function clear() { while (app.firstChild) app.removeChild(app.firstChild); }

  // ---- login -------------------------------------------------------------
  function showLogin(message) {
    clear();
    var wrap = el("div", "login");
    wrap.appendChild(el("h1", null, "Riven"));
    wrap.appendChild(el("p", "sub", "Sign in to your library"));
    if (message) wrap.appendChild(el("p", "err", message));

    var u = el("input"); u.type = "text"; u.value = USERNAME; u.placeholder = "Username";
    var p = el("input"); p.type = "password"; p.placeholder = "Password (API key)";
    var b = el("button", null, "Sign in");
    wrap.appendChild(u); wrap.appendChild(p); wrap.appendChild(b);
    app.appendChild(wrap);

    function submit() {
      b.disabled = true; b.textContent = "Signing in\u2026";
      api("/Users/AuthenticateByName", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ Username: u.value, Pw: p.value })
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 401 ? "Wrong username or password" : "Sign-in failed");
        return r.json();
      }).then(function (d) {
        writeCreds(d.ServerId, d.User.Id, d.AccessToken);
        showLibrary();
      }).catch(function (e) { showLogin(e.message || "Sign-in failed"); });
    }

    b.onclick = submit;
    p.onkeydown = function (ev) { if (ev.key === "Enter") submit(); };
  }

  // ---- library -----------------------------------------------------------
  function showLibrary() {
    clear();
    var head = el("div", "head");
    head.appendChild(el("h1", null, "Riven"));
    var out = el("button", "ghost", "Sign out");
    out.onclick = function () { window.localStorage.removeItem(CRED_KEY); showLogin(); };
    head.appendChild(out);
    app.appendChild(head);

    var status = el("p", "sub", "Loading library\u2026");
    app.appendChild(status);

    var grid = el("div", "grid");
    app.appendChild(grid);

    api("/Users/" + userId() + "/Items?Recursive=true&IncludeItemTypes=Movie&limit=300&sortBy=DateCreated&sortOrder=Descending")
      .then(function (r) {
        if (r.status === 401) { window.localStorage.removeItem(CRED_KEY); showLogin("Session expired"); return null; }
        if (!r.ok) throw new Error("Could not load library (" + r.status + ")");
        return r.json();
      })
      .then(function (d) {
        if (!d) return;
        var items = d.Items || [];
        if (!items.length) { status.textContent = "Library is empty."; return; }
        status.parentNode.removeChild(status);
        for (var i = 0; i < items.length; i++) grid.appendChild(tile(items[i]));
      })
      .catch(function (e) { status.textContent = e.message; status.className = "err"; });
  }

  function tile(item) {
    var card = el("div", "card");
    card.tabIndex = 0;

    var img = el("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = "/Items/" + item.Id + "/Images/Primary";
    img.onerror = function () { img.style.visibility = "hidden"; };
    card.appendChild(img);

    var meta = el("div", "meta");
    meta.appendChild(el("div", "title", item.Name || "Untitled"));
    var sub = [];
    if (item.ProductionYear) sub.push(item.ProductionYear);
    if (item.Studios && item.Studios[0]) sub.push(item.Studios[0].Name);
    meta.appendChild(el("div", "year", sub.join(" \u00b7 ")));
    card.appendChild(meta);

    function go() { play(item); }
    card.onclick = go;
    card.onkeydown = function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } };
    return card;
  }

  // ---- playback ----------------------------------------------------------
  function nativeAvailable() {
    try { return !!(window.NativePlayer && window.NativePlayer.isEnabled()); }
    catch (e) { return false; }
  }

  function play(item) {
    if (nativeAvailable()) {
      // PlayOptions.fromJson wants item IDs; ExoPlayer resolves the stream
      // itself via /Items/{id}/PlaybackInfo + /Videos/{id}/stream.
      window.NativePlayer.loadPlayer(JSON.stringify({
        ids: [item.Id], startIndex: 0, startPositionTicks: 0
      }));
      return;
    }
    browserPlay(item);
  }

  // Fallback for a plain browser (and for the app if the user selected the
  // web player instead of ExoPlayer).
  function browserPlay(item) {
    var back = el("div", "player");
    var v = document.createElement("video");
    v.controls = true;
    v.autoplay = true;
    v.src = "/Videos/" + item.Id + "/stream?static=true&api_key=" + encodeURIComponent(token());
    var close = el("button", "close", "\u2715");
    close.onclick = function () { v.pause(); document.body.removeChild(back); };
    back.appendChild(v); back.appendChild(close);
    document.body.appendChild(back);
  }

  // ---- boot --------------------------------------------------------------
  if (token()) showLibrary(); else showLogin();
})();
"""

STYLE_CSS = r"""
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #0d0f12; color: #e8eaed;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
}
h1 { font-size: 1.5rem; margin: 0; }
.sub { color: #9aa0a6; margin: .5rem 0 1rem; }
.err { color: #ff6b6b; }
.head { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem .25rem; }
.ghost { background: transparent; border: 1px solid #3c4043; color: #9aa0a6; border-radius: 6px; padding: .4rem .7rem; font-size: .85rem; }
.login { max-width: 22rem; margin: 18vh auto; padding: 0 1.5rem; text-align: center; }
.login input {
  display: block; width: 100%; margin: .6rem 0; padding: .85rem 1rem; font-size: 1rem;
  background: #17191d; border: 1px solid #3c4043; border-radius: 8px; color: #e8eaed;
}
.login button {
  width: 100%; margin-top: .8rem; padding: .9rem; font-size: 1rem; font-weight: 600;
  background: #00a4dc; border: 0; border-radius: 8px; color: #fff;
}
.login button[disabled] { opacity: .6; }
.grid {
  display: grid; gap: 1rem; padding: 1rem 1.25rem 2.5rem;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
}
/* Bigger tiles on a TV, where this is viewed from across a room. */
@media (min-width: 1000px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1.5rem; }
}
.card { cursor: pointer; border-radius: 10px; outline: none; transition: transform .12s ease; }
.card img { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: 10px; background: #17191d; display: block; }
.card:focus, .card:hover { transform: scale(1.05); }
.card:focus img { outline: 3px solid #00a4dc; outline-offset: 2px; }
.title { font-size: .85rem; margin-top: .45rem; line-height: 1.25;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.year { font-size: .75rem; color: #9aa0a6; }
.player { position: fixed; inset: 0; background: #000; z-index: 50; display: flex; align-items: center; }
.player video { width: 100%; max-height: 100%; }
.close { position: absolute; top: 1rem; right: 1rem; background: rgba(0,0,0,.6); color: #fff;
  border: 0; border-radius: 50%; width: 2.5rem; height: 2.5rem; font-size: 1.1rem; }
"""


def index_html() -> str:
    """The document the shell loads at `/`."""

    return INDEX_HTML.replace("__BUNDLE__", BUNDLE_PATH)


def bundle_js() -> str:
    """The application, served at the path that marks the client connected."""

    username = settings_manager.settings.jellyfin_server.username

    # The API key is deliberately NOT baked in: this route is unauthenticated
    # (it has to be -- the client fetches it before it has any credentials),
    # so the page asks for the key at login instead of shipping it to anyone
    # who requests the URL.
    return (
        "var __rivenStyle=document.createElement('style');"
        "__rivenStyle.textContent=" + _js_string(STYLE_CSS) + ";"
        "document.head.appendChild(__rivenStyle);\n"
        + BUNDLE_JS.replace("__USERNAME__", username.replace("\\", "\\\\").replace('"', '\\"'))
    )


def _js_string(value: str) -> str:
    """Embed CSS in a JS string literal without a template engine."""

    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        + '"'
    )
