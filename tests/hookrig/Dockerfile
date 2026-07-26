# Hermetic test image for the Claude Code <-> KB session hooks.
#
# DELIBERATELY MINIMAL. The hooks' transport-selection chain keys off the
# PRESENCE of tools (`uv`, `claude`) and files (~/.claude.json, pyproject.toml),
# so the image must ship NONE of them: every precondition is supplied per-case
# by a fake, which is the only way to toggle the priority chain deterministically.
#
# Present:  bash curl jq python3 git gzip/coreutils util-linux(setsid) procps
# Absent (asserted at build time): claude, uv, kb, anything from a host ~/.claude
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        git \
        gzip \
        jq \
        procps \
        python3 \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# Tripwire: if a future edit pulls in Claude Code or uv, every mode-priority
# case silently changes meaning. Fail the build instead.
RUN if command -v claude >/dev/null 2>&1 || command -v uv >/dev/null 2>&1 \
       || command -v kb >/dev/null 2>&1; then \
      echo "BUILD ABORTED: image ships claude/uv/kb; the rig must supply fakes"; exit 1; \
    fi

RUN useradd --create-home --uid 1000 --shell /bin/bash tester

USER tester
ENV HOME=/home/tester \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /home/tester
CMD ["bash"]
