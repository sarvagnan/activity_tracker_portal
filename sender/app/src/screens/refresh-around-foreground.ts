/** Begin local observations before network work, without letting a slow check hold up Sync. */
export async function refreshAroundForeground(
  foreground: () => Promise<unknown>,
  refresh: () => Promise<unknown>,
  checkPermissions: () => Promise<unknown>,
): Promise<void> {
  // The async wrapper also isolates synchronous throws so every observation starts.
  const observe = async (check: () => Promise<unknown>) => { await check(); };
  void Promise.allSettled([observe(refresh), observe(checkPermissions)]);
  try { await foreground(); }
  finally { await Promise.all([observe(refresh), observe(checkPermissions)]); }
}
