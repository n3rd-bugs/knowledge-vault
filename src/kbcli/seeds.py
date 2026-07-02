"""kbcli.seeds — `kb seed` corpus: 8+ real items across ≥3 domains (Phase 1).

Idempotent by construction: ingest dedupes on content hash, the fixes relation
upserts. Content is real engineering knowledge, not lorem ipsum, because the
eval golden set queries against it.
"""

from __future__ import annotations

from kbcore import graph, ingest

SEEDS: list[dict] = [
    {
        "project": "motor-ctl",
        "type": "decision",
        "title": "TPS55288 chosen for the buck-boost stage",
        "tags": ["hw:tps55288", "domain:power", "lang:c"],
        "code_ref": {"repo": "motor-ctl", "path": "hw/power/buck_boost.sch"},
        "body": """## Context
The motor controller needs 12 V ±5% from a battery input that swings 6–18 V,
so neither a pure buck nor a pure boost topology covers the range.

## Decision
Use the TI TPS55288 4-switch buck-boost converter. It covers 2.7–36 V input,
delivers up to 16 A, and exposes output voltage, current limit and slew rate
over I2C, which lets firmware trim the rail without BOM changes.

## Alternatives
- LM5175: pin-strapped only, no I2C telemetry, larger external FETs.
- Discrete SEPIC: cheaper magnetics but poor efficiency at 8 A and a much
  harder layout.

## Consequences
The TPS55288 I2C registers must be initialised before enabling the output
(VOUT defaults to 5 V); firmware owns the rail sequencing.""",
    },
    {
        "project": "motor-ctl",
        "type": "gotcha",
        "title": "DRV8706 SPI frames need mode 1 — polarity mismatch reads all zeros",
        "tags": ["hw:drv8706", "protocol:spi", "lang:c"],
        "code_ref": {
            "repo": "motor-ctl",
            "path": "src/drv8706.c",
            "lines": "88-131",
            "symbol": "drv_spi_write",
        },
        "body": """The DRV8706-Q1 gate driver samples SDI on the **falling** edge of SCLK
(CPOL=0, CPHA=1 — SPI mode 1). The ATmega328P SPI peripheral defaults to
mode 0, and with that setting every status read returns 0x0000 while writes
appear to succeed. No fault flag is raised — the part just ignores the frame.

Fix in `drv_spi_write`: set `SPCR |= (1<<CPHA)` before asserting nSCS, and
keep SCLK below 10 MHz. The same mode-1 requirement applies to the DRV8353
family, so treat any TI motor gate driver as mode 1 until the datasheet says
otherwise. Symptom to remember: SPI reads all zeros + no fault = clock
polarity/phase mismatch, not a dead chip.""",
    },
    {
        "project": "docx-tools",
        "type": "snippet",
        "title": "Node.js: add a footer with page numbers to a .docx (docx package)",
        "tags": ["lang:js", "framework:node-docx", "domain:tooling"],
        "code_ref": {
            "repo": "docx-tools",
            "path": "src/footer.js",
            "lines": "10-42",
            "symbol": "buildFooter",
        },
        "body": """Adding "Page X of Y" to every page with the `docx` npm package: the footer
must be attached per-section, and page numbers are field codes, not text.

```js
const { Document, Footer, Paragraph, TextRun, PageNumber, AlignmentType } = require("docx");

function buildFooter() {
  return new Footer({
    children: [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [
          new TextRun({ children: ["Page ", PageNumber.CURRENT] }),
          new TextRun({ children: [" of ", PageNumber.TOTAL_PAGES] }),
        ],
      }),
    ],
  });
}

const doc = new Document({
  sections: [{ footers: { default: buildFooter() }, children: [/* body */] }],
});
```

Gotcha: `PageNumber.CURRENT` must be inside the `children` array of a
`TextRun` — passing it as `text:` renders the literal string.""",
    },
    {
        "project": "infra",
        "type": "runbook",
        "title": "Debugging a systemd user service that keeps restarting",
        "tags": ["tool:systemd", "os:ubuntu"],
        "body": """## Symptoms
`systemctl --user status foo` shows `activating (auto-restart)` in a loop.

## Steps
1. `journalctl --user -u foo -n 100 --no-pager` — read the actual crash, not
   the unit state. Exit code 203 = ExecStart binary not found; 217 = bad User=.
2. `systemd-analyze --user verify foo.service` — catches unit-file typos.
3. Check `EnvironmentFile=` paths exist and are readable; a missing env file
   fails the unit before ExecStart runs.
4. If the service needs the user session after logout:
   `loginctl enable-linger $USER` (required once per machine).
5. Watch a fix live: `systemctl --user daemon-reload && systemctl --user
   restart foo && journalctl --user -fu foo`.

## Notes
`Restart=on-failure` masks crashes during boot-storms; set
`RestartSec=5` and `StartLimitIntervalSec=60` so the journal stays readable.""",
    },
    {
        "project": "motor-ctl",
        "type": "bug",
        "title": "UART RX drops bytes at 115200 baud during PWM bursts",
        "tags": ["protocol:uart", "hw:atmega328p", "lang:c"],
        "code_ref": {"repo": "motor-ctl", "path": "src/uart.c", "lines": "40-95"},
        "body": """Telemetry frames arriving at 115200 baud lose 1–3 bytes whenever the motor
PWM duty cycle changes rapidly. The UART RX ISR is starved: the PWM update
handler runs with interrupts disabled for ~120 µs (register reload + deadtime
recalculation), which exceeds the 86 µs byte time at 115200. UDR0 overruns
(DOR0 set) and silently discards bytes. Checksum failures showed up first in
the field as intermittent telemetry gaps, not as CRC errors.""",
    },
    {
        "project": "motor-ctl",
        "type": "note",
        "title": "Fix for UART byte loss: ring buffer + shortened PWM critical section",
        "tags": ["protocol:uart", "hw:atmega328p", "lang:c"],
        "code_ref": {
            "repo": "motor-ctl",
            "path": "src/uart.c",
            "lines": "40-95",
            "commit": "4f2c9a1",
            "symbol": "uart_rx_isr",
        },
        "body": """Resolved the 115200-baud UART RX byte loss: the PWM reload handler no longer
disables global interrupts — it snapshots the duty registers and lets the
UART RX ISR preempt it. The RX ISR now only pushes UDR0 into a 128-byte ring
buffer (4 instructions), and frame parsing moved to the main loop. Worst-case
interrupt latency dropped from ~120 µs to ~9 µs, comfortably inside the 86 µs
byte time. Soak test: 48 h at full PWM churn, zero DOR0 overruns.""",
    },
    {
        "project": "infra",
        "type": "config",
        "title": "Ollama GPU sharing: short keep_alive so models unload between calls",
        "tags": ["tool:ollama", "hw:rtx4080", "domain:ml"],
        "body": """On the shared RTX 4080S box, every Ollama API call passes an explicit
`keep_alive` instead of relying on the 5 m default:

```json
{"model": "bge-m3", "input": ["..."], "keep_alive": "2m"}
```

- Embedders (bge-m3, ~1.2 GB) use `2m` — cheap to reload, and daytime LLM
  workloads get the VRAM back quickly.
- The 14B extractor runs only in the nightly window with `keep_alive: 0` so
  it unloads immediately after the batch.
- `OLLAMA_MAX_LOADED_MODELS=1` prevents the embedder and extractor from
  stacking. Check current residency with `ollama ps`.""",
    },
    {
        "project": "teaching",
        "type": "note",
        "title": "بینن کی سلطنت — سبق کے نوٹس (Kingdom of Benin lesson notes)",
        "tags": ["domain:history", "lang:urdu"],
        "body": """بینن کی سلطنت (Kingdom of Benin) مغربی افریقہ میں موجودہ نائیجیریا کے علاقے
میں ایک طاقتور سلطنت تھی جو تقریباً 1180ء سے 1897ء تک قائم رہی۔

اہم نکات:
- دارالحکومت بینن سٹی اپنی مٹی کی فصیلوں کے لیے مشہور تھا — یہ دنیا کے سب سے
  بڑے قدیم تعمیراتی منصوبوں میں شمار ہوتی ہیں۔
- کانسی کی تختیاں (Benin Bronzes) شاہی محل کی زینت تھیں اور آج نوآبادیاتی
  لوٹ مار کی واپسی کی بحث کا مرکز ہیں۔
- اوبا (Oba) بادشاہ کا لقب تھا؛ اوبا ایوارے اول کے دور میں سلطنت اپنے عروج
  پر پہنچی۔

سبق کے لیے سوال: بینن کی کانسی کی تختیاں یورپی عجائب گھروں تک کیسے پہنچیں،
اور انہیں واپس کرنے کے حق میں اور خلاف کیا دلائل ہیں؟""",
    },
]

# (fix_title, rel, bug_title): the note fixes the bug (DATA-10).
SEED_RELATIONS: list[tuple[str, str, str]] = [
    (
        "Fix for UART byte loss: ring buffer + shortened PWM critical section",
        "fixes",
        "UART RX drops bytes at 115200 baud during PWM bursts",
    ),
]

SEED_PROJECT_DOMAINS = {
    "motor-ctl": "embedded",
    "docx-tools": "web",
    "infra": "devops",
    "teaching": "teaching",
}


def run_seed() -> tuple[int, int]:
    """Ingest the seed corpus. Returns (new_items, skipped_duplicates)."""
    from kbcore.items import find_by_title, set_project

    new, skipped = 0, 0
    for spec in SEEDS:
        item_id = ingest.ingest(
            spec["project"],
            spec["type"],
            spec["title"],
            spec["body"],
            kind="manual",
            tags=spec.get("tags"),
            code_ref=spec.get("code_ref"),
        )
        if item_id is None:
            skipped += 1
        else:
            new += 1
    for name, domain in SEED_PROJECT_DOMAINS.items():
        set_project(name, domain=domain)
    for src_title, rel, dst_title in SEED_RELATIONS:
        src_id = find_by_title(src_title)
        dst_id = find_by_title(dst_title)
        if src_id and dst_id:
            graph.link(src_id, rel, dst_id)
    return new, skipped
