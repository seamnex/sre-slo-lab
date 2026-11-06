"""Generador de carga: tráfico constante contra /api/checkout.

Constante a propósito. Con tráfico variable, un cambio en la tasa de errores
se confunde con un cambio en el volumen, y medir el tiempo de detección deja
de ser reproducible.
"""

import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = os.environ.get("URL", "http://servicio:8080/api/checkout")
RPS = float(os.environ.get("RPS", "10"))


def pedir():
    try:
        with urllib.request.urlopen(URL, timeout=5) as respuesta:
            respuesta.read()
    except (urllib.error.URLError, OSError):
        # Los errores los cuenta el servicio en sus métricas; acá solo importa
        # no frenar el ritmo de la carga.
        pass


def main():
    print(f"carga: {RPS} req/s contra {URL}", flush=True)
    intervalo = 1 / RPS
    proximo = time.monotonic()
    # Varios hilos: si un pedido se pone lento por una falla de latencia, la
    # carga no baja junto con él. Un cliente real tampoco espera al anterior.
    with ThreadPoolExecutor(max_workers=32) as hilos:
        while True:
            hilos.submit(pedir)
            proximo += intervalo
            time.sleep(max(0, proximo - time.monotonic()))


if __name__ == "__main__":
    main()
