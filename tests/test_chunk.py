"""Chunker unit tests (DATA-4): fences/tables atomic, breadcrumbs, code splitter."""

from __future__ import annotations

from kbcore.chunk import MAX_TOKENS, chunk_markdown, chunk_source, count_tokens

FENCE = "```c\nint main(void) {\n    return 0;\n}\n```"


def test_code_fence_survives_chunking_intact():
    filler = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 40).strip()
    body = f"# Setup\n\n{filler}\n\n{FENCE}\n\n{filler}"
    chunks = chunk_markdown("Doc", body)
    assert any(FENCE in c for c in chunks), "fence must appear verbatim in exactly one chunk"
    for c in chunks:
        assert ("```c" in c) == ("```" in c and "int main" in c) or "```" not in c


def test_oversize_fence_is_never_split():
    huge = "```python\n" + "\n".join(f"x{i} = {i}  # padding line" for i in range(600)) + "\n```"
    chunks = chunk_markdown("Doc", f"intro paragraph\n\n{huge}")
    matching = [c for c in chunks if "x599" in c]
    assert len(matching) == 1
    assert "x0 = 0" in matching[0], "start and end of the fence stay together"
    assert count_tokens(matching[0]) > MAX_TOKENS  # oversize permitted


def test_table_is_atomic():
    table = "| pin | net |\n|-----|-----|\n| 1 | VCC |\n| 2 | GND |"
    chunks = chunk_markdown("Doc", f"before\n\n{table}\n\nafter")
    assert any(table in c for c in chunks)


def test_breadcrumb_carries_heading_path():
    body = "# Hardware\n\n## Power\n\nThe buck-boost stage uses the TPS55288."
    chunks = chunk_markdown("Board notes", body)
    assert chunks and chunks[0].startswith("Board notes › Hardware › Power\n\n")


def test_c_function_splitter_keeps_function_whole():
    body_lines = "\n".join(f"    x = {i}; /* body line for realistic size */" for i in range(80))
    fn_a = (
        "static int drv_spi_write(uint8_t reg, uint16_t val) {\n"
        + body_lines
        + "\n    return 0;\n}"
    )
    fn_b = "void uart_rx_isr(void) {\n    ring_push(UDR0);\n}"
    chunks = chunk_source("drv8706.c", f"{fn_a}\n\n{fn_b}\n")
    containing_a = [c for c in chunks if "drv_spi_write" in c]
    assert len(containing_a) == 1
    assert "return 0;\n}" in containing_a[0], "function start and end in the same chunk"
    assert any("uart_rx_isr" in c for c in chunks)


def test_python_def_splitter():
    src = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    chunks = chunk_source("mod.py", src)
    assert any("def alpha" in c for c in chunks)
    assert any("def beta" in c for c in chunks)


def test_empty_body_gives_no_chunks():
    assert chunk_markdown("t", "") == []
