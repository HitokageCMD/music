/* tunebox — on-device store.
 *
 * IndexedDB blobs, not the Cache API. Both can hold the audio, but a cached
 * Response only replays as a 200: to make the seek bar work the service worker
 * would have to parse Range headers and synthesise 206s out of the stored body.
 * A blob URL hands the whole file to the browser instead, and seeking then costs
 * exactly zero lines of code.
 *
 * Stores: audio (Blob) · art (Blob) · meta ({...track, size, savedAt, lyrics})
 */

const IDB = (() => {
  const NAME = 'tunebox';
  const VERSION = 1;
  const STORES = ['audio', 'art', 'meta'];
  let dbp = null;

  function open() {
    if (dbp) return dbp;
    dbp = new Promise((res, rej) => {
      const req = indexedDB.open(NAME, VERSION);
      req.onupgradeneeded = () => {
        for (const s of STORES) {
          if (!req.result.objectStoreNames.contains(s)) req.result.createObjectStore(s);
        }
      };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
    return dbp;
  }

  function run(store, mode, fn) {
    return open().then((db) => new Promise((res, rej) => {
      const t = db.transaction(store, mode);
      const req = fn(t.objectStore(store));
      t.oncomplete = () => res(req ? req.result : undefined);
      t.onerror = () => rej(t.error);
      t.onabort = () => rej(t.error);
    }));
  }

  return {
    get: (s, k) => run(s, 'readonly', (o) => o.get(k)),
    put: (s, k, v) => run(s, 'readwrite', (o) => o.put(v, k)),
    del: (s, k) => run(s, 'readwrite', (o) => o.delete(k)),
    keys: (s) => run(s, 'readonly', (o) => o.getAllKeys()),
    all: (s) => run(s, 'readonly', (o) => o.getAll()),
  };
})();

const Offline = {
  async ids() {
    try { return new Set(await IDB.keys('meta')); } catch { return new Set(); }
  },

  async list() {
    try {
      return (await IDB.all('meta')).sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
    } catch { return []; }
  },

  /** Pull a track onto this device: audio, cover, and lyrics. */
  async save(t, onProgress) {
    const res = await fetch(`/api/stream/${t.id}`);
    if (!res.ok) throw new Error(`下载失败 (${res.status})`);

    const total = +res.headers.get('content-length') || 0;
    const reader = res.body.getReader();
    const chunks = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.length;
      if (total && onProgress) onProgress(got / total);
    }
    const blob = new Blob(chunks, { type: res.headers.get('content-type') || 'audio/mp4' });
    await IDB.put('audio', t.id, blob);

    // Cover and lyrics are small and useless to fetch later with no network.
    try {
      const a = await fetch(`/api/art/${t.id}`);
      if (a.ok) await IDB.put('art', t.id, await a.blob());
    } catch {}
    let lyrics = null;
    try { lyrics = await (await fetch(`/api/lyrics/${t.id}`)).json(); } catch {}

    await IDB.put('meta', t.id, {
      ...t, size: blob.size, savedAt: Date.now(), lyrics, cached: true, onPhone: true,
    });
    return blob.size;
  },

  async remove(id) {
    await Promise.all([IDB.del('audio', id), IDB.del('art', id), IDB.del('meta', id)]);
  },

  /** Blob URL for local audio, or null. Caller owns it — revoke when done. */
  async audioURL(id) {
    try {
      const b = await IDB.get('audio', id);
      return b ? URL.createObjectURL(b) : null;
    } catch { return null; }
  },

  async artURL(id) {
    try {
      const b = await IDB.get('art', id);
      return b ? URL.createObjectURL(b) : null;
    } catch { return null; }
  },

  async lyrics(id) {
    try { return (await IDB.get('meta', id))?.lyrics || null; } catch { return null; }
  },

  async usage() {
    const metas = await this.list();
    let quota = 0;
    try { quota = (await navigator.storage?.estimate?.())?.quota || 0; } catch {}
    return { tracks: metas.length, bytes: metas.reduce((s, m) => s + (m.size || 0), 0), quota };
  },

  /** Ask the browser not to evict us under storage pressure. */
  async persist() {
    try { return await navigator.storage?.persist?.() ?? false; } catch { return false; }
  },
};
