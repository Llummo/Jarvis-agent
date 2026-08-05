"""Drive a real browser, and record what it asked the server for.

The QA flow could already navigate and photograph a page. It could not click,
type, read the DOM, or see which endpoints the page actually called — so it
could report that a route answers, never that a feature works.

This adds the missing half. Two capabilities matter beyond the obvious:

**Network capture.** A ticket declares a route *and* an endpoint. Watching the
traffic the page generates is what turns «the form looks right» into «the form
called POST /api/v1/talent/people and got a 201». That is the difference
between checking a screenshot and checking behaviour, and it is why the CDP
event stream is collected instead of discarded.

**Finding by what a human sees.** `#form > div:nth-child(3) > button` breaks on
the first redesign. «the button that reads Guardar» survives it, so text and
accessible names are the primary way to locate things here.

Same CDP plumbing the screenshot tool already uses — no new browser stack.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_harness.mcp_server.cdp_screenshot import (
    _find_chromium,
    _open_new_tab,
    _wait_for_devtools_port,
)

DEFAULT_TIMEOUT_S = 25.0
# How long to keep re-checking a condition before calling it false. Long enough
# for an app that boots asynchronously, short enough that a genuinely broken
# page fails the step rather than the whole run.
DEFAULT_WAIT_S = 12.0
POLL_INTERVAL_S = 0.15
# How many consecutive polls the text must stay the same length before the
# page counts as settled. Two is enough to skip a progressive render and
# cheap enough not to add a visible pause.
RENDER_STABLE_POLLS = 2
# Minimum quiet time after the first content before the page counts as
# ready. Matches the screenshot tool, whose captures are already complete.
RENDER_SETTLE_GRACE_S = 0.8
# Below this a candidate is not the thing asked for. Tuned so «Agregar nueva
# persona» reaches «Agregar persona» (0.90) but never «Importar empleados» (0).
MIN_MATCH_SCORE = 0.6


class BrowserError(RuntimeError):
    """The browser could not do what was asked."""


@dataclass
class NetworkCall:
    """One request the page made, and how it went."""

    method: str
    url: str
    status: Optional[int] = None
    resource_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400

    @property
    def path(self) -> str:
        """The path alone, so an expectation can be written without the host —
        the same test has to run against local and the deployed QA site."""
        without_scheme = self.url.split("://", 1)[-1]
        slash = without_scheme.find("/")
        if slash == -1:
            return "/"
        return without_scheme[slash:].split("?", 1)[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "path": self.path,
            "status": self.status,
            "resource_type": self.resource_type,
            "ok": self.ok,
        }


@dataclass
class Browser:
    """A headless Chromium under CDP control.

    Use as a context manager; the profile directory and the process are cleaned
    up even if a step raises.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S
    _profile: Path = field(default=None, init=False, repr=False)
    _proc: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _loop: Any = field(default=None, init=False, repr=False)
    _ws: Any = field(default=None, init=False, repr=False)
    _msg_id: int = field(default=0, init=False, repr=False)
    _requests: Dict[str, NetworkCall] = field(default_factory=dict, init=False, repr=False)

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Browser":
        self._profile = Path(tempfile.mkdtemp(prefix="mh-qa-"))
        self._loop = asyncio.new_event_loop()
        chromium = _find_chromium()
        self._proc = subprocess.Popen(
            [
                str(chromium), "--headless=new", "--disable-gpu", "--no-sandbox",
                "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
                "--window-size=1280,900", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = _wait_for_devtools_port(self._profile, self._proc, timeout_s=self.timeout_s)
        ws_url = _open_new_tab(port, self.timeout_s)
        self._loop.run_until_complete(self._connect(ws_url))
        self._call("Page.enable")
        self._call("Network.enable")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ws is not None:
                self._loop.run_until_complete(self._ws.close())
        finally:
            if self._loop is not None:
                self._loop.close()
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self._profile is not None:
                shutil.rmtree(self._profile, ignore_errors=True)

    async def _connect(self, ws_url: str) -> None:
        import websockets

        self._ws = await websockets.connect(ws_url, max_size=None)

    async def _send(self, method: str, params: Optional[dict] = None) -> dict:
        """Send one command and wait for its reply, keeping the events.

        The reply and the event stream share one socket. Anything arriving
        without an `id` is an event; dropping those would throw away exactly
        the network traffic this class exists to record.
        """
        self._msg_id += 1
        msg_id = self._msg_id
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.timeout_s)
            message = json.loads(raw)
            if message.get("id") == msg_id:
                return message
            if "method" in message:
                self._absorb_event(message)

    def _absorb_event(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        request_id = params.get("requestId")
        if not request_id:
            return

        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            self._requests[request_id] = NetworkCall(
                method=str(request.get("method") or ""),
                url=str(request.get("url") or ""),
                resource_type=str(params.get("type") or ""),
            )
        elif method == "Network.responseReceived":
            call = self._requests.get(request_id)
            if call is not None:
                response = params.get("response") or {}
                call.status = response.get("status")
                call.resource_type = str(params.get("type") or call.resource_type)

    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        return self._loop.run_until_complete(self._send(method, params))

    # -- navigation and inspection ---------------------------------------

    def goto(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})
        # `readyState === 'complete'` means the document finished loading, not
        # that the app finished rendering. A React app mounts after that, so
        # reading the page here returns an empty body — which is how a login
        # screen slips through a check that looks for its text.
        self.wait_for("document.readyState === 'complete'", message=f"{url} never finished loading")
        self._wait_for_render()

    def _wait_for_render(self, *, timeout_s: float = DEFAULT_WAIT_S) -> None:
        """Wait until the page has stopped filling itself in.

        A first signal is not enough: the shell renders its header before the
        form, so returning on «there is some text» reads a page that is still
        arriving. This waits for the visible text to stop growing across
        consecutive polls.

        Returns quietly on timeout — a genuinely blank page is a valid state,
        and the caller's own assertions should be what fails, not this.
        """
        deadline = time.monotonic() + timeout_s
        previous, stable, first_content_at = -1, 0, None
        while time.monotonic() < deadline:
            try:
                length = self.eval(
                    "return ((document.body && document.body.innerText) || '').trim().length;"
                )
            except BrowserError:
                return
            if length and first_content_at is None:
                first_content_at = time.monotonic()

            if length and length == previous:
                stable += 1
                # Stability alone is not enough: a shell that renders its
                # header and then pauses looks settled for as long as the pause
                # lasts. Nothing is accepted until the grace period has run
                # from the first content, so late-arriving markup is not missed.
                settled_long_enough = (
                    first_content_at is not None
                    and time.monotonic() - first_content_at >= RENDER_SETTLE_GRACE_S
                )
                if stable >= RENDER_STABLE_POLLS and settled_long_enough:
                    return
            else:
                stable = 0
            previous = length
            time.sleep(POLL_INTERVAL_S)

    def eval(self, expression: str) -> Any:
        """Evaluate JS in the page and return the result."""
        payload = self._call(
            "Runtime.evaluate",
            {"expression": f"(() => {{ {expression} }})()", "returnByValue": True, "awaitPromise": True},
        )
        result = (payload.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error":
            raise BrowserError(str(result.get("description") or "JS error")[:300])
        return result.get("value")

    def wait_for(self, expression: str, *, timeout_s: float = DEFAULT_WAIT_S, message: str = "") -> None:
        """Poll a JS condition until it is true, or give up."""
        deadline = time.monotonic() + timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self.eval(f"return Boolean({expression});"):
                    return
            except BrowserError as exc:
                last_error = str(exc)
            time.sleep(POLL_INTERVAL_S)
        detail = message or f"condition never became true: {expression}"
        raise BrowserError(f"{detail}{f' ({last_error})' if last_error else ''}")

    # -- locating things the way a person would --------------------------

    def find_best_match(self, text: str, *, role: str = "") -> Optional[Dict[str, Any]]:
        """The element that best matches `text`, or None if none is close enough.

        Written against how tickets are actually worded. A ticket says «Agregar
        nueva persona»; the button reads «Agregar persona». Demanding the label
        contain the phrase verbatim fails that, and the failure looks like a
        missing button rather than a wording difference — which sends whoever
        reads the report hunting for a bug that is not there.

        So the match is scored, not literal: accents and case are folded, and
        the words of one have to be contained in the other. A genuinely wrong
        target still scores near zero and still fails, which is the point —
        «Agregar nueva persona» must not quietly match «Importar empleados».

        Returns the selector, the text actually found, the score, and whether
        it was exact, so a loose match can be reported rather than hidden.
        """
        script = r"""
        const wanted = %s;
        const role = %s;
        const MIN = %s;

        const fold = (s) => (s || '')
          .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
          .toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
        // Palabras de una o dos letras son artículos y preposiciones: dejarlas
        // haría que casi cualquier par de etiquetas se pareciera.
        const words = (s) => fold(s).split(' ').filter((w) => w.length > 2);

        const wantedFolded = fold(wanted);
        const wantedWords = words(wanted);

        // Cuándo dos palabras cuentan como la misma pese a no escribirse igual.
        function sameWord(a, b) {
          if (a === b) return true;
          const [short, long] = a.length <= b.length ? [a, b] : [b, a];
          // Prefijo de cuatro letras o más: cubre singular/plural («persona» /
          // «personas») sin llegar a emparejar «casa» con «casarse».
          if (short.length >= 4 && long.startsWith(short)) return true;
          // Una letra de diferencia: un typo del ticket no debe hacer que el
          // clic caiga en otro elemento. Solo en palabras largas, donde una
          // letra no cambia el significado.
          if (short.length >= 5 && long.length - short.length <= 1) {
            return editDistance(a, b) <= 1;
          }
          return false;
        }

        // Damerau-Levenshtein: cuenta una transposición como una sola edición.
        // Intercambiar dos letras seguidas («impotrar» por «importar») es el
        // error de tecleo más común, y con Levenshtein simple contaría como dos.
        function editDistance(a, b) {
          const m = a.length, n = b.length;
          const d = [];
          for (let i = 0; i <= m; i++) d.push(new Array(n + 1).fill(0));
          for (let i = 0; i <= m; i++) d[i][0] = i;
          for (let j = 0; j <= n; j++) d[0][j] = j;
          for (let i = 1; i <= m; i++) {
            for (let j = 1; j <= n; j++) {
              const cost = a[i - 1] === b[j - 1] ? 0 : 1;
              d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
              if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
                d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
              }
            }
          }
          return d[m][n];
        }

        function score(label) {
          const f = fold(label);
          if (!f) return 0;
          if (f === wantedFolded) return 1;
          // Deliberadamente NO se compara por subcadena: «es» aparece dentro de
          // «pesroan» y emparejaría el selector de idioma con letras revueltas.
          // Solo cuentan palabras completas.
          const lw = words(label);
          if (!lw.length || !wantedWords.length) return 0;
          const shared = lw.filter((w) => wantedWords.some((x) => sameWord(w, x))).length;
          if (!shared) return 0;
          // Asimétrico a propósito: se mide contra lo que pidió el ticket, no
          // contra la etiqueta. Que al botón le sobren palabras es normal
          // («Agregar» encuentra «Agregar persona»); que le falte una de las
          // pedidas no lo es — «Eliminar persona» emparejaría con «Personas»
          // y el paso haría clic en el menú en vez de fallar.
          const asked = shared / wantedWords.length;
          const coverage = shared / Math.max(lw.length, wantedWords.length);
          return 0.7 * asked + 0.3 * coverage;
        }

        const tags = role ? [role]
          : ['button', 'a', 'input', 'select', 'textarea', 'label', '[role=button]'];
        let pool = tags.map((t) => [...document.querySelectorAll(t)]).flat();
        if (!pool.length) pool = [...document.querySelectorAll('*')];

        // Con un diálogo abierto, el mismo texto suele existir dos veces: el
        // botón que lo abrió sigue detrás. En «Agregar persona» los dos se
        // llaman igual, así que sin esto el paso que debía enviar el formulario
        // vuelve a pulsar el de la barra y el diálogo nunca se envía.
        const dialog = document.querySelector('[role=dialog], dialog[open], [aria-modal="true"]');

        // Cómo se llama un elemento para quien lo mira. Un <input> no tiene
        // innerText y casi nunca aria-label: su nombre está en el <label> que
        // lo acompaña. Sin resolverlo, «Nombres *» no encontraba el campo que
        // literalmente se llama así, y el paso fallaba como si no existiera.
        function nameOf(el) {
          const direct = el.innerText || el.getAttribute('aria-label');
          if (direct && direct.trim()) return direct;
          const tag = el.tagName.toLowerCase();
          if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') {
            return el.value || '';
          }
          if (el.id) {
            const tied = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (tied && tied.innerText.trim()) return tied.innerText;
          }
          const wrapping = el.closest('label');
          if (wrapping && wrapping.innerText.trim()) return wrapping.innerText;
          return el.placeholder || el.name || el.value || '';
        }

        let best = null;
        for (const el of pool) {
          const label = nameOf(el) || '';
          const box = el.getBoundingClientRect();
          if (box.width === 0 || box.height === 0) continue;
          const s = score(label);
          if (s < MIN) continue;
          const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
          // Se ordena por un rango, no por la puntuación: a igualdad de texto
          // gana el que está dentro del diálogo, y pierde el deshabilitado.
          // Este último se devuelve igualmente para poder decir por qué no se
          // pudo hacer clic, en vez de dar el paso por bueno.
          const rank = s + (dialog && dialog.contains(el) ? 0.5 : 0) - (disabled ? 0.25 : 0);
          if (!best || rank > best.rank) {
            best = { el, rank: rank, score: s, disabled: disabled, text: (label || '').trim() };
          }
        }
        if (!best) return null;

        let selector;
        if (best.el.id) {
          selector = '#' + CSS.escape(best.el.id);
        } else {
          document.querySelectorAll('[data-mh-found]').forEach((e) => e.removeAttribute('data-mh-found'));
          best.el.setAttribute('data-mh-found', '1');
          selector = '[data-mh-found="1"]';
        }
        return { selector: selector, text: best.text, score: best.score,
                 disabled: best.disabled, exact: best.score >= 0.999 };
        """ % (json.dumps(text), json.dumps(role), MIN_MATCH_SCORE)
        return self.eval(script)

    def find_by_text(self, text: str, *, role: str = "") -> Optional[str]:
        """A CSS selector for the best match, or None."""
        found = self.find_best_match(text, role=role)
        return found["selector"] if found else None

    def visible(self, selector: str) -> bool:
        return bool(
            self.eval(
                f"const el = document.querySelector({json.dumps(selector)});"
                " if (!el) return false;"
                " const b = el.getBoundingClientRect();"
                " return !el.hidden && b.width > 0 && b.height > 0;"
            )
        )

    def text_of(self, selector: str = "body") -> str:
        return self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " return el ? (el.innerText || '') : '';"
        ) or ""

    def page_contains(self, text: str) -> bool:
        return text.strip().lower() in self.text_of().lower()

    # -- acting ----------------------------------------------------------

    def click(self, selector: str) -> None:
        """Click, refusing to pretend a disabled control did anything.

        `el.click()` on a disabled button returns quietly and changes nothing,
        so without this check the step passes, the next one fails somewhere
        unrelated, and the report blames the wrong thing. Sigo disables the
        save button until the required fields are filled, which is exactly the
        case a test is most likely to hit.
        """
        outcome = self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return 'missing';"
            " if (el.disabled === true || el.getAttribute('aria-disabled') === 'true')"
            "   return 'disabled';"
            " el.click(); return 'ok';"
        )
        if outcome == "missing":
            raise BrowserError(f"nothing to click at {selector!r}")
        if outcome == "disabled":
            raise BrowserError(f"el elemento en {selector!r} está deshabilitado")

    def type_into(self, selector: str, text: str) -> None:
        """Type as a person would, so a React form actually registers it.

        Assigning `el.value` directly does not work on a controlled input:
        React keeps its own value tracker, sees no change, and swallows the
        `input` event. The field then *looks* filled while the component's
        state stays empty — which showed up as Sigo's save button refusing to
        enable, and was very nearly filed as a defect in Sigo.

        Going through the prototype's native setter is what a real keystroke
        does, and updates the tracker along with it.
        """
        typed = self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false;"
            " el.focus();"
            " const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype"
            "   : (el instanceof HTMLSelectElement ? HTMLSelectElement.prototype"
            "   : HTMLInputElement.prototype);"
            " const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
            f" setter.call(el, {json.dumps(text)});"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return true;"
        )
        if not typed:
            raise BrowserError(f"no field to type into at {selector!r}")

    def set_cookie(self, name: str, value: str, url: str) -> None:
        """Install a cookie for `url`.

        The admin app authenticates with a cookie, not a bearer token in
        storage, so this is what actually makes a session. Uses CDP rather
        than document.cookie because the real cookie is httpOnly and
        JavaScript cannot write it.
        """
        result = self._call(
            "Network.setCookie", {"name": name, "value": value, "url": url}
        )
        if not (result.get("result") or {}).get("success", True):
            raise BrowserError(f"el navegador rechazó la cookie {name!r}")

    def screenshot(self, path: Path) -> Path:
        payload = self._call("Page.captureScreenshot", {"format": "png"})
        data = (payload.get("result") or {}).get("data")
        if not data:
            raise BrowserError("the browser returned no screenshot data")
        import base64

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    # -- what the page asked the server for ------------------------------

    def network_calls(self) -> List[NetworkCall]:
        """Every request the page made since the last reset, in order."""
        return list(self._requests.values())

    def api_calls(self) -> List[NetworkCall]:
        """Only the calls that look like an API, not assets.

        A page loads dozens of images, fonts and scripts. None of that is what
        a ticket's endpoint expectation is about.
        """
        return [
            call
            for call in self._requests.values()
            if call.resource_type.lower() in ("xhr", "fetch") or "/api/" in call.path
        ]

    def reset_network(self) -> None:
        """Forget traffic so far — called between steps so each one is judged
        on what it caused, not on what the page did while loading."""
        self._requests.clear()
