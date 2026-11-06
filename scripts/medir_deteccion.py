"""Mide cuánto tarda el sistema de alertas en detectar una falla inyectada.

    python scripts/medir_deteccion.py disponibilidad   # 20 % de errores 5xx
    python scripts/medir_deteccion.py latencia         # +350 ms a cada checkout
    python scripts/medir_deteccion.py caida            # se detiene el servicio

Para cada escenario registra cuatro momentos, todos con reloj de pared:

    inyección    -> se aplica la falla
    pending      -> Prometheus ve la condición de la alerta por primera vez
    firing       -> la alerta supera su `for:` y pasa a disparada
    notificación -> el aviso llega al notificador (el "pager")

y después retira la falla y mide cuánto tarda en llegar el aviso de resolución.
El tiempo de detección que importa es inyección -> notificación: una alerta
"firing" que nunca llegó a nadie no detectó nada.

Solo usa la biblioteca estándar. Supone el stack de docker compose levantado.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

PROMETHEUS = "http://localhost:9090"
SERVICIO = "http://localhost:8080"
NOTIFICADOR = "http://localhost:9099"

ESCENARIOS = {
    "disponibilidad": {"alerta": "SLOQuemaRapida", "slo": "disponibilidad", "falla": {"tasa_error": 0.2}},
    "latencia": {"alerta": "SLOQuemaRapida", "slo": "latencia", "falla": {"latencia_extra_ms": 350}},
    "caida": {"alerta": "SLOSinDatos", "slo": None, "falla": None},
}


def pedir(url, metodo="GET", cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as respuesta:
        return json.loads(respuesta.read() or b"null")


def ahora():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def estado_alerta(nombre, slo):
    """Devuelve 'pending', 'firing' o None para la alerta en Prometheus."""
    for alerta in pedir(f"{PROMETHEUS}/api/v1/alerts")["data"]["alerts"]:
        etiquetas = alerta["labels"]
        if etiquetas.get("alertname") == nombre and (slo is None or etiquetas.get("slo") == slo):
            return alerta["state"]
    return None


def notificacion(nombre, slo, estado, desde):
    """Primera notificación recibida con ese estado después de `desde`."""
    for n in pedir(NOTIFICADOR):
        recibida = datetime.fromisoformat(n["recibida"]).timestamp()
        if n["alerta"] == nombre and n["estado"] == estado and recibida >= desde:
            if slo is None or (n.get("resumen") or "").find(slo) >= 0:
                return recibida, n.get("canal")
    return None


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


def inyectar(escenario):
    if escenario["falla"] is None:
        compose("stop", "servicio")
    else:
        pedir(f"{SERVICIO}/admin/falla", "POST", {**escenario["falla"], "duracion_s": 3600})


def retirar(escenario):
    if escenario["falla"] is None:
        compose("start", "servicio")
    else:
        pedir(f"{SERVICIO}/admin/falla", "DELETE")


def esperar(condicion, limite_s, cada_s=2):
    fin = ahora() + limite_s
    while ahora() < fin:
        resultado = condicion()
        if resultado:
            return resultado
        time.sleep(cada_s)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("escenario", choices=ESCENARIOS)
    parser.add_argument("--limite", type=int, default=1800, help="segundos máximos de espera por etapa")
    parser.add_argument(
        "--sin-resolucion",
        action="store_true",
        help="retira la falla sin esperar el aviso de resolución (lo usa el CI)",
    )
    args = parser.parse_args()
    esc = ESCENARIOS[args.escenario]
    nombre, slo = esc["alerta"], esc["slo"]

    if estado_alerta(nombre, slo):
        sys.exit(f"{nombre} ya está activa: esperá a que se resuelva antes de medir.")

    t = {"inyeccion": ahora()}
    inyectar(esc)
    print(f"[{iso(t['inyeccion'])}] falla inyectada ({args.escenario})", flush=True)

    t["pending"] = esperar(lambda: estado_alerta(nombre, slo) in ("pending", "firing") and ahora(), args.limite)
    if not t["pending"]:
        sys.exit(f"{nombre} no pasó a pending en {args.limite} s")
    print(f"[{iso(t['pending'])}] {nombre} pending", flush=True)
    t["firing"] = esperar(lambda: estado_alerta(nombre, slo) == "firing" and ahora(), args.limite)
    if not t["firing"]:
        sys.exit(f"{nombre} no pasó a firing en {args.limite} s")
    print(f"[{iso(t['firing'])}] {nombre} firing", flush=True)
    recibida = esperar(lambda: notificacion(nombre, slo, "firing", t["inyeccion"]), args.limite)
    if not recibida:
        sys.exit(f"la notificación de {nombre} no llegó en {args.limite} s")
    t["notificacion"], canal = recibida
    print(f"[{iso(t['notificacion'])}] notificación recibida por el canal '{canal}'", flush=True)

    t["retiro"] = ahora()
    retirar(esc)
    print(f"[{iso(t['retiro'])}] falla retirada", flush=True)
    resuelta = None
    if not args.sin_resolucion:
        resuelta = esperar(lambda: notificacion(nombre, slo, "resolved", t["retiro"]), args.limite, cada_s=5)
    t["resuelta"] = resuelta[0] if resuelta else None
    print(f"[{iso(t['resuelta']) if t['resuelta'] else '--:--:--'}] aviso de resolución", flush=True)

    def delta(a, b):
        return round(t[b] - t[a], 1) if t.get(a) and t.get(b) else None

    resultado = {
        "escenario": args.escenario,
        "alerta": nombre,
        "canal": canal,
        "inyeccion_a_pending_s": delta("inyeccion", "pending"),
        "inyeccion_a_firing_s": delta("inyeccion", "firing"),
        "inyeccion_a_notificacion_s": delta("inyeccion", "notificacion"),
        "retiro_a_resolucion_s": delta("retiro", "resuelta"),
    }
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
