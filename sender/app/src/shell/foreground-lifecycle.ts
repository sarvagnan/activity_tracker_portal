/** Node-testable AppState wiring; a start and each transition back to active. */
export function startForegroundLifecycle(
  appState: {
    readonly currentState: string | null;
    addEventListener(event: "change", listener: (state: string) => void): { remove(): void };
  },
  foreground: () => Promise<unknown>,
  onError: () => void,
): () => void {
  let previous = appState.currentState;
  const invoke = () => { void Promise.resolve().then(foreground).catch(onError); };
  const subscription = appState.addEventListener("change", (state) => {
    const returned = state === "active" && previous !== "active";
    previous = state;
    if (returned) invoke();
  });
  invoke();
  return () => subscription.remove();
}
