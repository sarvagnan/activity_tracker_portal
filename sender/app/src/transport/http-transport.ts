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

export function createHttpTransport(options: HttpTransportOptions): HttpTransport {
  void options;
  throw new Error("receipt-bound-delivery@1 is not implemented");
}
