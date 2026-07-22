FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Node 22 (latchkey requires node >= 20; Debian bookworm ships 18), plus the
# graphical stack for browser-based logins: Chromium on a virtual display
# (Xvfb) streamed to the owner via x11vnc + noVNC (over the app's own
# websocket bridge).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends \
        nodejs \
        chromium \
        xvfb \
        x11vnc \
        matchbox-window-manager \
        fonts-liberation \
        fonts-noto-color-emoji \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

ARG LATCHKEY_VERSION=2.21.0
RUN npm install -g latchkey@${LATCHKEY_VERSION}

# noVNC client (static files served by the app on the browser-login page).
ARG NOVNC_VERSION=1.5.0
RUN mkdir -p /opt/novnc \
    && curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz \
       | tar -xz --strip-components=1 -C /opt/novnc

WORKDIR /app

# Install Python dependencies (source is copied first so the project itself
# builds during `uv sync`).
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen --no-dev

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV DISPLAY=:99

EXPOSE 8080

CMD ["./entrypoint.sh"]
