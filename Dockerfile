FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner_core.py /app/scanner_core.py
COPY server.py /app/server.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV PORT=8000
EXPOSE 8000

CMD ["/app/start.sh"]
