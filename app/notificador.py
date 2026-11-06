"""Receptor de webhooks de Alertmanager.

Hace de "pager": registra cada notificación con la hora en que llegó. Es el
último eslabón de la cadena, y el que define el tiempo de detección real: una
alerta que está "firing" en Prometheus pero nunca le llegó a nadie no detectó nada.
"""

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = int(os.environ.get("PUERTO", "9099"))
RECIBIDAS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", "0"))
        cuerpo = json.loads(self.rfile.read(largo) or b"{}")
        llegada = datetime.now(timezone.utc).isoformat()
        for alerta in cuerpo.get("alerts", []):
            registro = {
                "recibida": llegada,
                # /pager o /ticket: muestra que el ruteo por severidad de
                # Alertmanager mandó cada alerta al receptor correcto.
                "canal": self.path.strip("/"),
                "estado": alerta.get("status"),
                "alerta": alerta.get("labels", {}).get("alertname"),
                "severidad": alerta.get("labels", {}).get("severity"),
                "desde": alerta.get("startsAt"),
                "resumen": alerta.get("annotations", {}).get("summary"),
            }
            RECIBIDAS.append(registro)
            print(json.dumps(registro, ensure_ascii=False), flush=True)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        # El script de medición lee de acá las notificaciones recibidas.
        datos = json.dumps(RECIBIDAS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Handler).serve_forever()
