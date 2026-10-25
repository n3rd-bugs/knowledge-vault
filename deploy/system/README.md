# System-level units (root, `/etc/systemd/system`)

`scripts/install_systemd.sh` installs every file in `systemd/` as a **user** unit. The
files here are the owner box's **system** units and drop-ins, which that script never
touches. They are checked in so the box can be rebuilt; install them by hand:

```bash
sudo install -m644 deploy/system/kb-rerank.service /etc/systemd/system/
sudo install -D -m644 deploy/system/kb-rerank.service.d/cache-ram.conf \
     /etc/systemd/system/kb-rerank.service.d/cache-ram.conf
sudo install -D -m644 deploy/system/ollama.service.d/override.conf \
     /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama kb-rerank
```

| File | Why |
|---|---|
| `ollama.service.d/override.conf` | one model slot (`OLLAMA_MAX_LOADED_MODELS=1`) and `OLLAMA_NUM_PARALLEL=1` — see the comments in the file |
| `kb-rerank.service` | Qwen3-Reranker sidecar on `172.17.0.1:8324`, run from a **pinned** llama-server runtime under `/opt/kb-rerank/` |
| `kb-rerank.service.d/cache-ram.conf` | host-RAM prompt cache off (RSS 9.4 GB → 1.8 GB) |

## The reranker runtime is pinned, not Ollama's

`kb-rerank` used to run `/usr/local/lib/ollama/llama-server`. Ollama's installer replaces
that directory wholesale, so an Ollama upgrade silently swapped the reranker's llama.cpp
build — or stranded `GGML_BACKEND_PATH`, which falls back to the CPU without an error
(15 s vs 0.4 s per call, then the fail-open rerank leg hides it). The runtime is now a
copy (`cp -a`) of the directory that shipped with Ollama 0.31.1 at
`/opt/kb-rerank/llama-0.31.1/`; the binaries have `RUNPATH=$ORIGIN`, so every library
resolves inside it. Check with:

```bash
sudo grep -oE '/[^ ]+\.so[^ ]*' /proc/$(systemctl show -p MainPID --value kb-rerank)/maps | sort -u
```

Changing that runtime is its own measured change: capture scores for a fixed request
before and after, and compare from the **second** call on — the first call after a
restart differs by up to ~0.1 (warm-up), later calls are bit-identical.

## Upgrading Ollama (manual tarball; never `install.sh`)

`install.sh` rewrites `/etc/systemd/system/ollama.service` (with whatever `PATH` the
calling shell had) and restarts the service mid-whatever-is-running. Do it by hand,
with the GPU idle and no `kb-consolidate` run active:

```bash
V=0.40.11; D=/var/tmp/ollama-$V
mkdir -p $D && cd $D
curl -fsSLO https://github.com/ollama/ollama/releases/download/v$V/sha256sum.txt
curl -fsSLO https://github.com/ollama/ollama/releases/download/v$V/ollama-linux-amd64.tar.zst
grep 'ollama-linux-amd64.tar.zst$' sha256sum.txt | sha256sum -c -
OLD=$(ollama -v | awk '{print $NF}')
sudo systemctl stop ollama
sudo mkdir -p /opt/ollama-rollback/$OLD
sudo mv /usr/local/bin/ollama /opt/ollama-rollback/$OLD/ollama
sudo mv /usr/local/lib/ollama /opt/ollama-rollback/$OLD/lib-ollama
sudo tar --zstd --no-same-owner -xf ollama-linux-amd64.tar.zst -C /usr/local
sudo systemctl start ollama
```

Rollback = stop the service, move the two paths back from `/opt/ollama-rollback/$OLD/`,
start it. Models live in `/usr/share/ollama/.ollama/models` and are not touched by
either direction.

After an upgrade, before the next nightly: every production model must load **100 %
GPU** at `num_ctx` 16384 (`ollama ps`, and the `projected to use` line in
`journalctl -u ollama`), and the extraction / judge / embedding parity benches must be
run against the previous version's numbers.
