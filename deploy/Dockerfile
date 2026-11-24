FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
COPY notifier.py official_bridge.py relay_state.py filters.json profile.md proposal_prompt.md ./
COPY deploy/run_notifier.py deploy/run_official_delivery.py ./deploy/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UPWORK_NOTIFIER_ENV=/run/notifier-config/env.json \
    SERVE_SECONDS=31536000 \
    STORE_PATH=/var/lib/upwork-notifier/store.json \
    SEEN_PATH=/var/lib/upwork-notifier/seen.json \
    LOCK_PATH=/var/lib/upwork-notifier/notifier.lock \
    BRIDGE_STATE_PATH=/var/lib/upwork-notifier/official-delivery.sqlite3
USER 1000:1000
CMD ["python", "deploy/run_notifier.py"]
