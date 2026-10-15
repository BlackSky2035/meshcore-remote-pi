// REST-Aufrufe. Fehler werfen eine Error mit der Meldung des Servers.

export async function api(path, opts, timeoutMs = 0) {
  let r;
  // Zeitlimit nur auf Wunsch (schnelle Lese-Abfragen): nach langer Pause im Hintergrund hängt am iPhone die erste Anfrage
  // oft auf einer toten Verbindung. Abbrechen und neu fragen ist dann viel schneller als auf das Netz-Zeitlimit zu warten.
  const ctl = timeoutMs && typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  try {
    r = await fetch(path, ctl ? {...opts, signal: ctl.signal} : opts);
  } catch {
    throw new Error(ctl?.signal.aborted ? 'Zeitüberschreitung – der Server hat nicht rechtzeitig geantwortet.'
      : 'Server nicht erreichbar. Läuft das Programm noch? (Verbindung wird automatisch neu versucht)');
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const d = (await r.json()).detail;
      msg = Array.isArray(d) ? d.map(x => x.msg).join('; ') : (d || msg);
    } catch { /* keine JSON-Antwort */ }
    if (r.status === 401 && path !== '/api/auth/login' && globalThis.onLoginRequired) globalThis.onLoginRequired();
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

const json = (method, body) => ({
  method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body ?? {}),
});

export const get = path => api(path);
// Lese-Abfrage mit Zeitlimit und EINEM neuen Versuch (nur für reine Datenbank-Abfragen, die nie lange dauern; nie für Funk-Aktionen)
export async function getQuick(path, timeoutMs = 8000) {
  try { return await api(path, undefined, timeoutMs); }
  catch (e) { if (e.status) throw e; return api(path, undefined, timeoutMs); }
}
export const post = (path, body) => api(path, json('POST', body));
export const put = (path, body) => api(path, json('PUT', body));
export const del = path => api(path, {method: 'DELETE'});
