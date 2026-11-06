"""Servicio de checkout instrumentado: el sistema sobre el que se miden los SLOs.

Expone una sola ruta de negocio (/api/checkout) y una API de administración
para inyectar fallas a demanda: una tasa de errores, latencia extra, o las dos.
Así el lab puede reproducir el mismo incidente las veces que haga falta y medir
cuánto tarda cada alerta en enterarse.
"""

import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

PUERTO = int(os.environ.get("PUERTO", "8080"))

# El límite del SLO de latencia (300 ms) es un bucket exacto del histograma. Si
# no lo fuera, "qué fracción de pedidos tardó menos de 300 ms" se calcularía
# interpolando entre dos buckets: el SLI dejaría de ser una cuenta y pasaría a
# ser una estimación, y nadie se daría cuenta mirando el dashboard.
BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5)

PEDIDOS = Counter(
    "http_requests_total",
    "Pedidos HTTP atendidos, por ruta y código.",
    ["route", "code"],
)
DURACION = Histogram(
    "http_request_duration_seconds",
    "Latencia de los pedidos HTTP.",
    ["route"],
    buckets=BUCKETS,
)


class Falla:
    """Estado de la falla inyectada, compartido entre los hilos del servidor."""

    def __init__(self):
        self._lock = threading.Lock()
        self.limpiar()

    def limpiar(self):
        with self._lock:
            self.tasa_error = 0.0
            self.latencia_extra_ms = 0
            self.hasta = 0.0
            self.desde = 0.0

    def configurar(self, tasa_error, latencia_extra_ms, duracion_s):
        if not 0.0 <= tasa_error <= 1.0:
            raise ValueError("tasa_error tiene que estar entre 0 y 1")
        if not 0 <= latencia_extra_ms <= 10_000:
            raise ValueError("latencia_extra_ms tiene que estar entre 0 y 10000")
        if not 0 < duracion_s <= 86_400:
            raise ValueError("duracion_s tiene que estar entre 1 y 86400")
        with self._lock:
            self.tasa_error = tasa_error
            self.latencia_extra_ms = latencia_extra_ms
            self.desde = time.time()
            self.hasta = self.desde + duracion_s

    def vigente(self):
        """Devuelve (tasa_error, latencia_extra_ms) de la falla activa, o ceros."""
        with self._lock:
            if time.time() >= self.hasta:
                return 0.0, 0
            return self.tasa_error, self.latencia_extra_ms

    def estado(self):
        with self._lock:
            activa = time.time() < self.hasta
            return {
                "activa": activa,
                "tasa_error": self.tasa_error if activa else 0.0,
                "latencia_extra_ms": self.latencia_extra_ms if activa else 0,
                "desde": self.desde if activa else None,
                "hasta": self.hasta if activa else None,
            }


FALLA = Falla()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Un log por pedido a 10 req/s tapa todo lo demás; las métricas ya
        # cuentan lo que importa.
        pass

    def _responder(self, codigo, cuerpo, tipo="application/json"):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        if self.path == "/api/checkout":
            self._checkout()
        elif self.path == "/healthz":
            self._responder(200, {"status": "ok"})
        elif self.path == "/metrics":
            self._responder(200, generate_latest(), CONTENT_TYPE_LATEST)
        elif self.path == "/admin/falla":
            self._responder(200, FALLA.estado())
        else:
            self._responder(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/admin/falla":
            self._responder(404, {"error": "not found"})
            return
        try:
            largo = int(self.headers.get("Content-Length", "0"))
            pedido = json.loads(self.rfile.read(largo) or b"{}")
            FALLA.configurar(
                float(pedido.get("tasa_error", 0)),
                int(pedido.get("latencia_extra_ms", 0)),
                int(pedido.get("duracion_s", 300)),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._responder(400, {"error": str(error)})
            return
        print(json.dumps({"evento": "falla_inyectada", **FALLA.estado()}), flush=True)
        self._responder(200, FALLA.estado())

    def do_DELETE(self):
        if self.path != "/admin/falla":
            self._responder(404, {"error": "not found"})
            return
        FALLA.limpiar()
        print(json.dumps({"evento": "falla_retirada", "ts": time.time()}), flush=True)
        self._responder(200, FALLA.estado())

    def _checkout(self):
        inicio = time.perf_counter()
        tasa_error, latencia_extra_ms = FALLA.vigente()

        # Latencia base: la mayoría rápida y una cola larga chica. Sin falla,
        # muy pocos pedidos pasan los 300 ms, así el SLO de latencia arranca sano.
        base = random.lognormvariate(-3.2, 0.6)  # mediana ~40 ms
        time.sleep(min(base, 1.0) + latencia_extra_ms / 1000)

        codigo = 500 if random.random() < tasa_error else 200
        self._responder(codigo, {"ok": codigo == 200})

        PEDIDOS.labels("/api/checkout", str(codigo)).inc()
        DURACION.labels("/api/checkout").observe(time.perf_counter() - inicio)


if __name__ == "__main__":
    print(json.dumps({"evento": "escuchando", "puerto": PUERTO}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Handler).serve_forever()
