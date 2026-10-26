FROM debian:bookworm-slim
# HEIGHT is derived in entrypoint from ASPECT_RATIO × WIDTH when unset (16:10 → 800).
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:1 ASPECT_RATIO=16:10 WIDTH=1280

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xfce4 xfce4-terminal dbus-x11 x11vnc novnc websockify \
    xdotool scrot xclip firefox-esr python3 python3-venv \
    fonts-dejavu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install fastapi uvicorn
RUN useradd -m -u 1000 agent

COPY server /opt/server
COPY entrypoint.sh /entrypoint.sh
COPY scripts/start-firefox.sh /opt/start-firefox.sh
COPY profile/user.js /home/agent/.mozilla/firefox/relay-agent/user.js
COPY profile/profiles.ini /home/agent/.mozilla/firefox/profiles.ini

RUN chmod +x /entrypoint.sh /opt/start-firefox.sh \
    && chown -R agent:agent /home/agent/.mozilla

USER agent
WORKDIR /home/agent
EXPOSE 6080 8000
ENTRYPOINT ["/entrypoint.sh"]
