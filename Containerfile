# Agent pod image: Claude Code + opencode + gh, run as uid 1000 ("agent").
# Build: podman build -t apod ~/src/rad/apod
FROM registry.fedoraproject.org/fedora:43

RUN dnf install -y git gh jq ripgrep python3 nodejs npm which procps-ng findutils \
        less curl tar gzip unzip diffutils \
    && dnf clean all

# Claude Code native binary, copied out of root's home so a mounted /home/agent can't hide it.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp -L /root/.local/bin/claude /usr/local/bin/claude \
    && rm -rf /root/.local /root/.claude /root/.claude.json

# opencode 2.x is published as @opencode/cli (opencode-ai stopped at 1.x).
RUN npm install -g @opencode/cli && npm cache clean --force

# Binaries live in the image; update by rebuilding.
ENV DISABLE_AUTOUPDATER=1

RUN useradd -m -u 1000 -d /home/agent agent
USER agent
WORKDIR /home/agent
