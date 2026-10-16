/** One HTTP request, in the shape of both OutboxRequest and EnrollmentRequest (receipt-bound-delivery@1). */
export interface HttpRequest {
  readonly method: "POST" | "GET";
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  /** The exact request bytes for `POST`; `null` for `GET`. */
  readonly body: Uint8Array | null;
  /** One deadline for the whole exchange, headers and body together. */
  readonly timeoutMs: number;
  /** At most this many body bytes plus one are kept. */
  readonly maxResponseBytes: number;
}

export type HttpResult =
  | { readonly outcome: "response"; readonly body: Uint8Array }
  | { readonly outcome: "failed" };

export interface HttpFetchInit {
  readonly method: "POST" | "GET";
  readonly headers: Record<string, string>;
  /** A fresh copy of the request bytes, so a fetch that reads late still sends them unchanged. */
  readonly body?: Uint8Array<ArrayBuffer>;
  readonly redirect: "error";
  readonly credentials: "omit";
  readonly signal: AbortSignal;
}

export interface HttpFetchResponse {
  readonly status: number;
  readonly redirected: boolean;
  readonly body: ReadableStream<Uint8Array> | null;
}

/** The WinterCG fetch this transport performs each request with: `expo/fetch` on the Sender, Node's in tests. */
export type HttpFetch = (url: string, init: HttpFetchInit) => Promise<HttpFetchResponse>;

export interface HttpTransportOptions {
  readonly fetch: HttpFetch;
}

/** Serves send-outbox-message@1's OutboxTransport and manage-sender-enrollment@1's EnrollmentTransport. */
export interface HttpTransport {
  send(request: HttpRequest): Promise<HttpResult>;
}

function validateRequest(request: HttpRequest): void {
  if (typeof request !== "object" || request === null ||
      (request.method !== "POST" && request.method !== "GET")) {
    throw new TypeError("Expected a POST or GET request");
  }
  if (typeof request.url !== "string") throw new TypeError("Expected an absolute HTTP URL");
  const url = new URL(request.url);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TypeError("Expected an absolute HTTP URL");
  }
  if (typeof request.headers !== "object" || request.headers === null || Array.isArray(request.headers) ||
      Object.values(request.headers).some((value) => typeof value !== "string")) {
    throw new TypeError("Expected headers with string values");
  }
  if (request.method === "POST" ? !(request.body instanceof Uint8Array) : request.body !== null) {
    throw new TypeError("Expected POST bytes or a null GET body");
  }
  if (!Number.isSafeInteger(request.timeoutMs) || request.timeoutMs <= 0 ||
      !Number.isSafeInteger(request.maxResponseBytes) || request.maxResponseBytes <= 0) {
    throw new TypeError("Expected positive safe integer timeoutMs and maxResponseBytes");
  }
}

export function createHttpTransport(options: HttpTransportOptions): HttpTransport {
  if (typeof options !== "object" || options === null || typeof options.fetch !== "function") {
    throw new TypeError("Expected an injected fetch function");
  }
  const fetch = options.fetch;
  return {
    async send(request) {
      validateRequest(request);
      const { method, url, timeoutMs, maxResponseBytes } = request;
      const controller = new AbortController();
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      let cancelled = false;
      const cancelRead = (): void => {
        if (reader === undefined || cancelled) return;
        cancelled = true;
        // A source may reject cancellation or never settle it; neither delays the result.
        try { void reader.cancel().catch(() => {}); } catch { /* best effort */ }
      };
      let timer: ReturnType<typeof setTimeout> | undefined;
      const deadline = new Promise<HttpResult>((resolve) => {
        timer = setTimeout(() => {
          controller.abort();
          cancelRead();
          resolve({ outcome: "failed" });
        }, timeoutMs);
      });
      try {
        // Snapshot caller-owned inputs before fetch can defer reading them.
        const init: HttpFetchInit = {
          method, headers: { ...request.headers },
          ...(method === "POST" ? { body: new Uint8Array(request.body!) } : {}),
          redirect: "error", credentials: "omit", signal: controller.signal,
        };
        const exchange = async (): Promise<HttpResult> => {
          let complete = false;
          try {
            const response = await fetch(url, init);
            if (controller.signal.aborted || typeof response !== "object" || response === null ||
                (response.status >= 300 && response.status < 400) || response.redirected ||
                response.body === null || typeof response.body?.getReader !== "function") {
              return { outcome: "failed" };
            }
            reader = response.body.getReader();
            const chunks: Uint8Array[] = [];
            let size = 0;
            const limit = maxResponseBytes + 1;
            while (true) {
              const { done, value } = await reader.read();
              if (controller.signal.aborted) return { outcome: "failed" };
              if (done) {
                complete = true;
                break;
              }
              if (!(value instanceof Uint8Array)) return { outcome: "failed" };
              const kept = value.subarray(0, limit - size);
              if (kept.byteLength > 0) {
                chunks.push(new Uint8Array(kept));
                size += kept.byteLength;
              }
              if (size === limit) {
                cancelRead();
                break;
              }
            }
            const body = new Uint8Array(size);
            let offset = 0;
            for (const chunk of chunks) {
              body.set(chunk, offset);
              offset += chunk.byteLength;
            }
            return { outcome: "response", body };
          } catch {
            return { outcome: "failed" };
          } finally {
            if (!complete) cancelRead();
            try { reader?.releaseLock(); } catch { /* cleanup must not reject send */ }
          }
        };
        return await Promise.race([exchange(), deadline]);
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
