// REST-Aufrufe. Fehler werfen eine Error mit der Meldung des Servers.

export async function api(path, opts) {
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new Error('Server nicht erreichbar. Läuft das Programm noch? (Verbindung wird automatisch neu versucht)');
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
export const post = (path, body) => api(path, json('POST', body));
export const put = (path, body) => api(path, json('PUT', body));
export const del = path => api(path, {method: 'DELETE'});
