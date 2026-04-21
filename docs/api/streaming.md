# Streaming (SSE)

Every long-running job publishes its stdout/stderr as Server-Sent Events. Tail a job with `GET /api/v1/stream/{job_id}`.

## The endpoint

```
GET /api/v1/stream/{job_id}
Authorization: Bearer <token>
Accept:        text/event-stream
```

Response headers include:

```
Content-Type:     text/event-stream
Cache-Control:    no-cache, no-transform
Connection:       keep-alive
X-Accel-Buffering: no
```

The `X-Accel-Buffering: no` directive tells an nginx reverse-proxy to forward frames as they arrive (the default for a lot of proxy setups is to buffer them up to flush size, which breaks live tailing).

## Frame format

Each line of subprocess output becomes a JSON payload wrapped in an SSE `event: line` frame:

```
event: line
data: {"line": "Analyzing readme.py…"}

event: line
data: {"line": "$ /usr/local/bin/gitoma run https://github.com/…"}
```

A **heartbeat comment** is emitted every 15 seconds during quiet periods:

```
: heartbeat
```

Comments are ignored by `EventSource` clients but count as traffic — they prevent reverse proxies from closing the idle connection.

## Replay + live

When a client connects, the server **replays** every line still in the ring buffer (capped at 500 lines) before switching to live tailing. Clients that reconnect don't need to manage history.

## Terminal sentinel

The server emits one final frame with a sentinel payload when the job ends:

```
event: line
data: {"line": "__END__:completed"}
```

The suffix is the job status: `completed`, `cancelled`, `timed_out`, or `failed[: detail]`. Clients should close the connection on receiving the sentinel.

## Back-pressure: drop-oldest

If a subscriber falls behind and its queue fills up, the server **drops the oldest buffered line** to make room for the new one — it does **not** block the producer. The client sees a small gap in the tail but keeps following live output. This mirrors standard log-tailing semantics and avoids the class of bugs where a slow client stalls every other consumer.

## Line truncation

Any single line longer than 4 KiB is truncated with `…(truncated)`. Prevents a runaway `print()` in the subprocess from blowing up the ring buffer or every subscriber queue.

## Credential redaction

Before a line is published, the server runs it through a sanitiser that replaces basic-auth credentials in git/ssh/https URLs:

```
fatal: could not read from https://x:ghp_abc@github.com/…
→
fatal: could not read from https://REDACTED@github.com/…
```

Pydantic validators at the `/run` boundary already reject credentialed URLs — this is defence-in-depth against the CLI printing authenticated clone URLs in its own stack traces.

## A minimal client

```js
const token = 'paste-from-gitoma-serve';

const res = await fetch(`/api/v1/stream/${jobId}`, {
  headers: { Authorization: `Bearer ${token}` },
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = '';
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const frames = buffer.split(/\n\n/);
  buffer = frames.pop();
  for (const frame of frames) {
    for (const line of frame.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const { line: text } = JSON.parse(line.slice(5).trim());
      console.log(text);
      if (text.startsWith('__END__')) return;
    }
  }
}
```

This is essentially the cockpit's own `LogStream` implementation. `EventSource` cannot carry custom headers, so the cockpit and clients that need Bearer auth use the fetch-stream pattern above.

## Cancellation

While the stream is open, `POST /api/v1/jobs/{job_id}/cancel` signals the underlying process group with `SIGTERM`, escalates to `SIGKILL` after a 5-second grace period, and emits `__END__:cancelled` on the stream. Clients should wait for the sentinel before flipping a UI "cancelled" state — the API's cancel response is merely an acknowledgement that the signal was sent.
