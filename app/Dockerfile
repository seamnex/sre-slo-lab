# Una sola imagen para los tres procesos de Python del lab (servicio, carga y
# notificador); cada servicio del compose elige cuál correr.
FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY servicio.py carga.py notificador.py ./

USER 65532:65532
