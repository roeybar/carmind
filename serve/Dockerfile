FROM python:3.12-slim

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin carmind

WORKDIR /app

COPY serve/requirements.txt serve/requirements-dev.txt ./
ARG DEV=0
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$DEV" = "1" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi \
    && pip check

COPY serve/app/ ./app/
COPY knowledge/ /kb/knowledge/
COPY schema/ /kb/schema/

ENV CARMIND_KB_DIR=/kb PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="carmind" \
      org.opencontainers.image.description="Warning-light triage for roadside-assistance calls: sourced verdicts from owner's-manual records, served over HTTP and MCP; a language model may pick from closed lists but never writes the verdict." \
      org.opencontainers.image.source="https://github.com/roeybar/carmind" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

USER carmind

EXPOSE 7600

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7600"]
