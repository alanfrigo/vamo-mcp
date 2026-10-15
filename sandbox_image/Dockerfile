FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 XAUTHORITY=/home/agent/.Xauthority HOME=/home/agent LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends chromium xvfb openbox x11vnc novnc websockify xdotool scrot imagemagick dbus-x11 xclip supervisor socat iproute2 curl wget git sudo python3 python3-pip python3-venv nodejs npm ca-certificates xauth x11-utils x11-xserver-utils fonts-dejavu-core fonts-liberation fonts-noto-color-emoji libxcb1 libxcb-shm0 libxcb-randr0 libx11-6 libxrandr2 libxfixes3 && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 1000 agent && useradd -m -u 1000 -g 1000 -s /bin/bash agent && mkdir /workspace && chown agent:agent /workspace && printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent
COPY agent_daemon/requirements.txt /opt/agent_daemon/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /opt/agent_daemon/requirements.txt
COPY agent_daemon/main.py /opt/agent_daemon/main.py
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
WORKDIR /opt/agent_daemon
ENTRYPOINT ["/entrypoint.sh"]
