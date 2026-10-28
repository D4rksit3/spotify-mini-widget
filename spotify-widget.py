#!/usr/bin/env python3
"""Mini widget de Spotify siempre encima: carátula, canción, controles y cola (máx. 10).

- Arrastrar con clic izquierdo para moverlo (recuerda la posición).
- Clic derecho: menú para cerrar o desconectar la cuenta.
- La cola usa la Web API de Spotify: pon tu Client ID en ~/.config/spotify-widget/config.json
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# En Wayland solo las ventanas X11 pueden pedir "siempre encima"
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

CONF_DIR = os.path.expanduser("~/.config/spotify-widget")
CONF = os.path.join(CONF_DIR, "config.json")
TOKEN = os.path.join(CONF_DIR, "token.json")

BUS = "org.mpris.MediaPlayer2.spotify"
PATH = "/org/mpris/MediaPlayer2"
IFACE = "org.mpris.MediaPlayer2.Player"

REDIRECT = "http://127.0.0.1:8898/callback"
SCOPE = "user-read-playback-state user-read-currently-playing"
ART = 56
THUMB = 32
MAX_QUEUE = 10

CSS = b"""
window { background-color: transparent; }
.card { background-color: rgba(22, 22, 22, 0.95); border-radius: 12px; padding: 8px; }
.title { color: #ffffff; font-weight: bold; }
.artist { color: #a7a7a7; font-size: 9pt; }
.card button { background: none; border: none; box-shadow: none; color: #ffffff;
               min-width: 24px; min-height: 24px; padding: 3px; border-radius: 14px; }
.card button:hover { background-color: rgba(255, 255, 255, 0.12); }
.card button.play { background-color: #1db954; color: #000000; }
.card button.play:hover { background-color: #1ed760; }
.queue, .queue row { background-color: transparent; }
.queue row { padding: 4px 6px; border-radius: 6px; color: #dddddd; font-size: 9pt; }
.queue row:hover { background-color: rgba(255, 255, 255, 0.08); }
.hint { color: #a7a7a7; font-size: 9pt; }
"""


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data, private=False):
    os.makedirs(CONF_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


class SpotifyAPI:
    """Solo para leer la cola: MPRIS no la expone."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.token = load_json(TOKEN, None)

    def authorized(self):
        return bool(self.token and self.token.get("refresh_token"))

    def logout(self):
        self.token = None
        try:
            os.remove(TOKEN)
        except OSError:
            pass

    def login(self):
        """Flujo PKCE: abre el navegador y espera el callback en 127.0.0.1 (bloqueante)."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [None])[0] == state:
                    result.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>Listo, ya puedes cerrar esta pestaña.</h2>".encode())

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 8898), Handler)
        srv.timeout = 2
        webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPE,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }))
        deadline = time.time() + 180
        try:
            while "code" not in result and "error" not in result and time.time() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        if "code" not in result:
            raise RuntimeError(result.get("error", "no se completó el inicio de sesión"))
        self._token_request({
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT,
            "client_id": self.client_id,
            "code_verifier": verifier,
        })

    def _token_request(self, form):
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        old_refresh = (self.token or {}).get("refresh_token")
        self.token = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", old_refresh),
            "expires_at": time.time() + data.get("expires_in", 3600) - 60,
        }
        save_json(TOKEN, self.token, private=True)

    def _access_token(self):
        if time.time() >= self.token.get("expires_at", 0):
            self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
                "client_id": self.client_id,
            })
        return self.token["access_token"]

    def queue(self):
        req = urllib.request.Request(
            "https://api.spotify.com/v1/me/player/queue",
            headers={"Authorization": "Bearer " + self._access_token()},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        items = []
        for it in (data.get("queue") or [])[:MAX_QUEUE]:
            if it.get("artists"):
                who = ", ".join(a["name"] for a in it["artists"])
            else:  # episodios de podcast
                who = (it.get("show") or {}).get("name", "")
            images = (it.get("album") or it).get("images") or []
            # Spotify ordena de mayor a menor; la más chica basta para la miniatura
            image = images[-1]["url"] if images else None
            items.append((it.get("name", "?"), who, image))
        return items


class Widget(Gtk.Window):
    def __init__(self):
        super().__init__(title="Spotify mini")
        self.config = load_json(CONF, {})
        if "client_id" not in self.config:
            self.config["client_id"] = ""
            save_json(CONF, self.config)
        self.api = SpotifyAPI(self.config["client_id"]) if self.config["client_id"] else None

        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.running = False
        self.track_id = None
        self.art_url = None
        self.art_cache = {}
        self.save_pos_id = 0

        self._setup_window()
        self._build_ui()

        Gio.bus_watch_name(Gio.BusType.SESSION, BUS, Gio.BusNameWatcherFlags.NONE,
                           self._on_appeared, self._on_vanished)
        # Spotify no siempre emite PropertiesChanged; sondeo suave como respaldo
        GLib.timeout_add(1500, self.refresh)

    # ---------- ventana ----------
    def _setup_window(self):
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
            self.set_app_paintable(True)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        pos = self.config.get("pos")
        if pos:
            self.move(*pos)
        else:
            geo = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
            area = geo.get_workarea()
            self.move(area.x + area.width - 360, area.y + 20)
        self.connect("configure-event", self._on_configure)
        self.connect("destroy", Gtk.main_quit)

    def _on_configure(self, *_):
        if self.save_pos_id:
            GLib.source_remove(self.save_pos_id)
        self.save_pos_id = GLib.timeout_add(600, self._save_pos)

    def _save_pos(self):
        self.save_pos_id = 0
        self.config["pos"] = list(self.get_position())
        save_json(CONF, self.config)
        return False

    # ---------- interfaz ----------
    def _button(self, icon, tip, cb, css=None):
        b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        b.set_tooltip_text(tip)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_valign(Gtk.Align.CENTER)
        if css:
            b.get_style_context().add_class(css)
        b.connect("clicked", cb)
        return b

    def _build_ui(self):
        card = Gtk.EventBox()
        card.get_style_context().add_class("card")
        card.connect("button-press-event", self._on_press)
        self.add(card)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add(outer)

        row = Gtk.Box(spacing=8)
        outer.pack_start(row, False, False, 0)

        self.cover = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.cover.set_size_request(ART, ART)
        row.pack_start(self.cover, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("title")
        self.artist = Gtk.Label(xalign=0)
        self.artist.get_style_context().add_class("artist")
        for lbl in (self.title, self.artist):
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(20)
            lbl.set_width_chars(20)
            text.pack_start(lbl, False, False, 0)
        row.pack_start(text, True, True, 0)

        self.play_btn = self._button("media-playback-start-symbolic", "Reproducir / pausa",
                                     lambda *_: self._call("PlayPause"), "play")
        row.pack_start(self._button("media-skip-backward-symbolic", "Anterior",
                                    lambda *_: self._call("Previous")), False, False, 0)
        row.pack_start(self.play_btn, False, False, 0)
        row.pack_start(self._button("media-skip-forward-symbolic", "Siguiente",
                                    lambda *_: self._call("Next")), False, False, 0)
        row.pack_start(self._button("view-list-symbolic", "A continuación",
                                    self._toggle_queue), False, False, 0)

        self.queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.queue_box.set_no_show_all(True)
        outer.pack_start(self.queue_box, False, False, 0)

        self.show_all()
        self._set_idle()

    def _on_press(self, widget, event):
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        elif event.button == 3:
            menu = Gtk.Menu()
            if self.api and self.api.authorized():
                item = Gtk.MenuItem(label="Desconectar cuenta (cola)")
                item.connect("activate", lambda *_: (self.api.logout(), self._load_queue()))
                menu.append(item)
            item = Gtk.MenuItem(label="Cerrar widget")
            item.connect("activate", lambda *_: self.destroy())
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
        return True

    # ---------- reproductor (MPRIS) ----------
    def _on_appeared(self, *_):
        self.running = True
        self.conn.signal_subscribe(BUS, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                   PATH, None, Gio.DBusSignalFlags.NONE, lambda *_: self.refresh())
        self.refresh()

    def _on_vanished(self, *_):
        self.running = False
        self._set_idle()

    def _call(self, method):
        if self.running:
            self.conn.call(BUS, PATH, IFACE, method, None, None,
                           Gio.DBusCallFlags.NONE, 2000, None, None, None)

    def _set_idle(self):
        self.track_id = None
        self.art_url = None
        self.title.set_text("Spotify no está abierto")
        self.artist.set_text("")
        self.cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)

    def refresh(self):
        if not self.running:
            return True
        try:
            props = self.conn.call_sync(
                BUS, PATH, "org.freedesktop.DBus.Properties", "GetAll",
                GLib.Variant("(s)", (IFACE,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        except GLib.Error:
            return True

        playing = props.get("PlaybackStatus") == "Playing"
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.play_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)

        meta = props.get("Metadata") or {}
        track_id = meta.get("mpris:trackid")
        if track_id and track_id != self.track_id:
            self.track_id = track_id
            self.title.set_text(meta.get("xesam:title") or "—")
            self.artist.set_text(", ".join(meta.get("xesam:artist") or []))
            self.title.set_tooltip_text(self.title.get_text())
            self._load_art(meta.get("mpris:artUrl"))
            if self.queue_box.get_visible():
                GLib.timeout_add(900, lambda: self._load_queue() and False)
        return True

    def _load_art(self, url):
        self.art_url = url
        if not url:
            self.cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            return

        def done(pix):
            if self.art_url == url:
                self.cover.set_from_pixbuf(pix)

        self._fetch_pixbuf(url, ART, done)

    def _fetch_pixbuf(self, url, size, done):
        """Descarga la imagen en segundo plano y llama a done(pixbuf) en el hilo de la interfaz."""
        if not url:
            return
        key = (url, size)
        if key in self.art_cache:
            done(self.art_cache[key])
            return

        def work():
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    data = r.read()
            except OSError:
                return
            GLib.idle_add(apply, data)

        def apply(data):
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
            except GLib.Error:
                return False
            pix = loader.get_pixbuf().scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
            self.art_cache[key] = pix
            done(pix)
            return False

        threading.Thread(target=work, daemon=True).start()

    # ---------- cola ----------
    def _toggle_queue(self, *_):
        if self.queue_box.get_visible():
            self.queue_box.hide()
            self.resize(1, 1)
        else:
            self.queue_box.show()
            self._load_queue()

    def _set_queue_content(self, widget):
        for child in self.queue_box.get_children():
            self.queue_box.remove(child)
        self.queue_box.pack_start(widget, False, False, 0)
        widget.show_all()
        self.resize(1, 1)

    def _hint(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(34)
        lbl.get_style_context().add_class("hint")
        return lbl

    def _load_queue(self):
        if not self.api:
            self._set_queue_content(self._hint(
                "Para ver la cola pon tu Client ID de Spotify en\n~/.config/spotify-widget/config.json"))
            return
        if not self.api.authorized():
            btn = Gtk.Button(label="Conectar con Spotify")
            btn.connect("clicked", self._login)
            self._set_queue_content(btn)
            return

        self._set_queue_content(self._hint("Cargando…"))

        def work():
            try:
                items, err = self.api.queue(), None
            except Exception as e:  # red, token revocado, etc.
                items, err = [], str(e)
            GLib.idle_add(self._show_queue, items, err)

        threading.Thread(target=work, daemon=True).start()

    def _show_queue(self, items, err):
        if err:
            self._set_queue_content(self._hint("No se pudo leer la cola: " + err))
            return False
        if not items:
            self._set_queue_content(self._hint("No hay nada a continuación."))
            return False
        lb = Gtk.ListBox()
        lb.get_style_context().add_class("queue")
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        for i, (name, who, image) in enumerate(items, 1):
            row = Gtk.Box(spacing=8)
            thumb = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
            thumb.set_size_request(THUMB, THUMB)
            self._fetch_pixbuf(image, THUMB, thumb.set_from_pixbuf)
            row.pack_start(thumb, False, False, 0)
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup('<span foreground="#1db954">{}</span>  {}  <span foreground="#a7a7a7">{}</span>'.format(
                i, GLib.markup_escape_text(name), GLib.markup_escape_text(who)))
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(36)
            row.pack_start(lbl, True, True, 0)
            lb.add(row)
        lb.connect("row-activated", lambda _lb, row: self._skip(row.get_index() + 1))
        self._set_queue_content(lb)
        return False

    def _skip(self, n):
        """Salta a la n-ésima de la cola pulsando 'siguiente' n veces."""
        left = [n]

        def step():
            self._call("Next")
            left[0] -= 1
            return left[0] > 0

        if step():
            GLib.timeout_add(350, step)

    def _login(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Autoriza en el navegador…")

        def work():
            try:
                self.api.login()
                GLib.idle_add(self._load_queue)
            except Exception as e:
                GLib.idle_add(self._show_queue, [], str(e))

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    Widget()
    Gtk.main()
