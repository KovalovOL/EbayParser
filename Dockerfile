FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-deps -r requirements.lock && useradd --uid 10001 --create-home signal && mkdir /data && chown signal:signal /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS app
COPY --chown=signal:signal app ./app
USER signal

FROM runtime AS browser-deps
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps --no-shell chromium \
    && mkdir -p /browser-profile \
    && chown signal:signal /browser-profile \
    && chmod 700 /browser-profile \
    && rm -rf /var/lib/apt/lists/*

FROM browser-deps AS browser
COPY --chown=signal:signal app ./app
USER signal

