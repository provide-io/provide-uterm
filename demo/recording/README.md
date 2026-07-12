# Session Recording

Every byte that flows through a session can be captured to a structured JSONL recording.
Snapshots of the terminal screen are stored alongside send/receive events so you can
reconstruct exactly what the terminal looked like at any point in time. Recordings are
downloadable for audit trails, compliance, or offline replay.

**What you'll see:** Library demos (Python / Go / C#) start a `LocalFile*` recording store,
append a short deploy story as **screen snapshots**, then print JSONL metadata and a sample
line. The multi-language asciinema casts prove each implementation persists the same
lifecycle (`log_start` → `snapshot`… → `log_stop`).

## Multi-language terminal casts

| Language | Cast | Store type |
|----------|------|------------|
| **Python** | [python/terminal.cast](python/terminal.cast) | `LocalFileRecordingStore` |
| **Go** | [go/terminal.cast](go/terminal.cast) | `recording.LocalFileStore` |
| **C#** | [csharp/terminal.cast](csharp/terminal.cast) | `Provide.Uterm.Recording.LocalFileStore` |

Legacy root [terminal.cast](terminal.cast) is refreshed from the **Python** matrix cast.

**Contract + diagrams:** [docs/operations/recording-store-parity.md](../../docs/operations/recording-store-parity.md)
(lifecycle sequence, cross-language map, query rules).

### Re-record the matrix

From the repo root (requires `asciinema`, `go`, `dotnet`):

```bash
uv run python -m scripts.demos.record_recording_matrix
# or a subset:
uv run python -m scripts.demos.record_recording_matrix --langs python,go,csharp
```

Demo programs (also runnable without asciinema):

```bash
uv run python scripts/demos/recording_matrix/demo_python.py
(cd packages/provide-uterm-go && go run ./cmd/demo-recording)
dotnet run --project packages/provide-uterm-csharp/cmd/RecordingDemo -c Release
```

### Thin server HTTP surface (annotate + meta/entries/download)

Same four REST routes as Python `routes/sessions.py`, on Go and C# servers:

```bash
(cd packages/provide-uterm-go && go run ./cmd/demo-recording-http)
dotnet run --project packages/provide-uterm-csharp/cmd/RecordingHttpDemo -c Release
```

See [recording-store-parity.md](../../docs/operations/recording-store-parity.md)
for the route table and auth capabilities.

## Full-stack browser demo (Python server)

The original server+browser recorder still lives in
`scripts/demos/record_recording.py` (FastAPI + hijack + replay UI).

| File | Description |
|------|-------------|
| [browser_trim.mp4](browser_trim.mp4) | Highlight clip (Python server UI) |
| [terminal.cast](terminal.cast) | Library-level Python cast (matrix) |
| [screenshots/](screenshots/) | Browser stills from full-stack demo |
