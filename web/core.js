(()=>{'use strict';
/* Grocyste browser transport. Privileged credentials remain on the server. */
function createGrocysteSdk({ windowObject = globalThis.window, base = '/__grocyste/v1', fetchImpl = windowObject.fetch.bind(windowObject) } = {}) {
  const modules = Object.create(null), registered = new Map(), stores = new Map(), compatCaches = new Map();
  let session = null, pendingSession = null, configuration = {}, available = false;
  const uuid = () => windowObject.crypto.randomUUID();
  const validId = id => /^[a-z][a-z0-9-]{0,63}$/.test(id);
  const error = (message, status = 0, code = '') => Object.assign(new Error(message), { status, code });
  async function wire(path, { method = 'GET', data, headers = {}, authenticated = true, idempotencyKey, signal } = {}) {
    if (authenticated) await authenticate();
    const supplied = new Headers(headers);
    supplied.set('Accept', 'application/json');
    if (data !== undefined) supplied.set('Content-Type', 'application/json');
    if (method !== 'GET' && authenticated) {
      supplied.set('X-Grocyste-CSRF', session.csrfToken);
      supplied.set('Idempotency-Key', idempotencyKey || uuid());
    }
    const response = await fetchImpl(`${base}${path}`, { method, headers: supplied, credentials: 'same-origin', redirect: 'error', ...(data === undefined ? {} : { body: JSON.stringify(data) }), signal: signal || windowObject.AbortSignal?.timeout(30000) });
    let payload;
    try { payload = await response.json(); } catch { throw error('Réponse Grocyste invalide', response.status); }
    if (!response.ok || payload?.ok === false) {
      if (response.status === 401) session = null;
      throw error(payload?.error?.message || payload?.error || 'Opération Grocyste refusée', response.status, payload?.code || '');
    }
    return payload;
  }
  async function authenticate() {
    if (session) return session;
    if (pendingSession) return pendingSession;
    pendingSession = wire('/auth/session', { method: 'POST', authenticated: false, data: {} }).then(data => {
      if (typeof data.csrfToken !== 'string' || !data.user) throw error('Connexion Grocy requise', 401);
      session = data; configuration = {...configuration,instanceConfig:data.instanceConfig||{},settings:data.settings||configuration.settings||{}}; available = true; return data;
    }).finally(() => { pendingSession = null; });
    return pendingSession;
  }
  async function initialize() {
    try {
      configuration = await wire('/public-config', { authenticated: false });
      await authenticate();
      return true;
    } catch { available = false; return false; }
  }
  function scope(addonId) {
    if (!validId(addonId)) throw new TypeError('Identifiant addon invalide');
    return Object.freeze({
      async request(method, path, data, options = {}) {
        const route = path.replace(/^\/+/, '');
        const result = await wire('/grocy/request', { method: 'POST', data: { addonId, method: method.toUpperCase(), path: route.startsWith('api/') ? route : `api/${route}`, ...(data === undefined ? {} : { data }), ...(options.raw ? { raw: true } : {}), ...(options.bodyEncoding ? { bodyEncoding: options.bodyEncoding, contentType: options.contentType } : {}) }, ...options });
        return options.raw ? result : result.data;
      },
      async runtime(operation, params = {}, options = {}) {
        return wire('/runtime/call', { method: 'POST', data: { addonId, operation, params }, ...options });
      },
      async getStorage(key) {
        const result = await wire(`/storage/${addonId}/${encodeURIComponent(key)}`);
        stores.set(`${addonId}/${key}`, result); return result;
      },
      async putStorage(key, value, options = {}) {
        const previous = stores.get(`${addonId}/${key}`) || await this.getStorage(key);
        const result = await wire(`/storage/${addonId}/${encodeURIComponent(key)}`, { method: 'PUT', data: { data: value }, headers: { 'If-Match': String(previous.revision ?? 0) }, ...options });
        stores.set(`${addonId}/${key}`, result); return result;
      },
      async events(after = 0) {
        if (!Number.isSafeInteger(after) || after < 0) throw new TypeError('Curseur d’événements invalide');
        return (await wire(`/events?addonId=${encodeURIComponent(addonId)}&after=${after}`)).events;
      },
      async credential(provider, secret) {
        if (String(secret).startsWith('grocyste-credential:')) return String(secret);
        await this.runtime('credential.store', { provider, secret });
        return `grocyste-credential:${provider}`;
      },
    });
  }
  const providerHosts = {
    'api.openai.com': 'openai', 'generativelanguage.googleapis.com': 'gemini', 'api.anthropic.com': 'anthropic',
    'api.cohere.com': 'cohere', 'api.mistral.ai': 'mistral', 'api.groq.com': 'groq', 'api.together.xyz': 'together',
    'openrouter.ai': 'openrouter', 'api.deepseek.com': 'deepseek', 'api.x.ai': 'xai', 'api.perplexity.ai': 'perplexity',
    'api.fireworks.ai': 'fireworks', 'router.huggingface.co': 'huggingface', 'api.cerebras.ai': 'cerebras',
    'models.inference.ai.azure.com': 'github-models',
  };
  function responseFrom(result) {
    const value = result.data?.body !== undefined ? result.data : result;
    let body = value.body ?? (value.data === undefined ? '' : JSON.stringify(value.data));
    if (value.bodyEncoding === 'base64') body = Uint8Array.from(windowObject.atob(body), char => char.charCodeAt(0));
    const status=value.status||200;return new Response([204,205,304].includes(status)?null:body, { status, headers: { 'Content-Type': value.contentType || 'application/json' } });
  }
  function compatFetch(addonId) {
    const client = scope(addonId);
    return async (input, options = {}) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, windowObject.location.href);
      const method = String(options.method || input?.method || 'GET').toUpperCase();
      const headers = new Headers(options.headers || input?.headers);
      headers.delete('GROCY-API-KEY'); headers.delete('X-NerdCore-Token');
      let body = options.body;
      if (url.origin === windowObject.location.origin && /\/api\//.test(url.pathname)) {
        const path = url.pathname.slice(url.pathname.indexOf('/api/') + 5) + url.search;
        if (typeof body === 'string' && /application\/json/i.test(headers.get('Content-Type') || '')) body = JSON.parse(body);
        let bodyEncoding;
        if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
          const bytes = body instanceof ArrayBuffer ? new Uint8Array(body) : new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
          body = windowObject.btoa(Array.from(bytes, value => String.fromCharCode(value)).join('')); bodyEncoding = 'base64';
        }
        const result = await client.request(method, path, body, { raw: true, signal: options.signal, bodyEncoding, contentType: headers.get('Content-Type') });
        return responseFrom(result);
      }
      if (url.origin === windowObject.location.origin && url.pathname.includes('/__nerdcore_update/')) {
        const path = url.pathname.split('/__nerdcore_update/')[1];
        const params = body ? JSON.parse(body) : {};
        const operations = { health: 'health', 'v1/runtime/health': 'health', 'v1/runtime/cache/clear': 'cache.clear', 'v1/runtime/barcode-search': 'barcode.search', 'v1/check': 'addons.check', 'v1/install': 'addons.install', 'v1/update': 'addons.install', 'v1/runtime/producthelper/courseu-import-state': method === 'GET' ? 'courseu.state.get' : 'courseu.state.upsert' };
        if (path === 'v1/public-config') return new Response(JSON.stringify({ ...configuration, ok: true }), { headers: { 'Content-Type': 'application/json' } });
        const operation = path.includes('receipt-memory') ? method === 'GET' ? 'receipt-memory.get' : 'receipt-memory.upsert' : operations[path];
        if (!operation) throw error('Route de compatibilité inconnue', 404);
        if (operation.startsWith('addons.')) {
          params.addonId = params.addonId || addonId;
          params.version = params.version || params.releaseTag?.replace(/^v/, '') || params.releaseTags?.[addonId]?.replace(/^v/, '');
        }
        return responseFrom({ status: 200, body: JSON.stringify(await (operation.startsWith('addons.') ? scope('grocyste') : client).runtime(operation, params)), contentType: 'application/json' });
      }
      if (url.origin === windowObject.location.origin && url.pathname.includes('/__mon_grocy/live/v1/')) {
        return responseFrom(await client.runtime('sessions.live', { path: url.pathname.split('/__mon_grocy/live/v1/')[1] + url.search, method, ...(body ? { data: JSON.parse(body) } : {}) }));
      }
      const provider = providerHosts[url.hostname];
      const auth = headers.get('Authorization'), querySecret = url.searchParams.get('key'), apiSecret = headers.get('x-api-key') || headers.get('api-key');
      let credentialRef;
      if (provider && (auth || querySecret || apiSecret)) {
        credentialRef = await client.credential(provider, querySecret || apiSecret || auth.replace(/^Bearer\s+/i, ''));
        headers.delete('Authorization'); headers.delete('x-api-key'); headers.delete('api-key'); url.searchParams.delete('key');
      } else if (auth || querySecret || apiSecret) throw error('Configurer ce fournisseur côté serveur dans Grocyste', 403);
      if (typeof body !== 'string' && body !== undefined) {
        if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
          const bytes = body instanceof ArrayBuffer ? new Uint8Array(body) : new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
          body = windowObject.btoa(Array.from(bytes, value => String.fromCharCode(value)).join(''));
        } else throw error('Format réseau non pris en charge', 400);
      }
      return responseFrom(await client.runtime('external.fetch', { url: url.href, method, headers: Object.fromEntries(headers), ...(body === undefined ? {} : { body }), ...(credentialRef ? { credentialRef } : {}) }, { signal: options.signal }));
    };
  }
  function compatApi(addonId) {
    const client = scope(addonId), api = {};
    for (const [name, method] of Object.entries({ Get: 'GET', Post: 'POST', Put: 'PUT', Patch: 'PATCH', Delete: 'DELETE' })) {
      api[name] = (path, data, done, failed) => {
        if (method === 'GET' || method === 'DELETE') { failed = done; done = data; data = undefined; }
        const promise = client.request(method, path, data);
        promise.then(done, failure => failed?.({ status: failure.status, responseJSON: { error_message: failure.message } }));
        return promise;
      };
    }
    api.UploadFile = (file, group, name, done, failed) => {
      const promise = Promise.resolve(file.arrayBuffer()).then(buffer => {
        const bytes=new Uint8Array(buffer), chunks=[];
        for(let index=0;index<bytes.length;index+=8192)chunks.push(String.fromCharCode(...bytes.subarray(index,index+8192)));
        return client.request('PUT',`files/${encodeURIComponent(group)}/${encodeURIComponent(name)}`,windowObject.btoa(chunks.join('')),{bodyEncoding:'base64',contentType:file.type||'application/octet-stream'});
      });
      promise.then(done,failure=>failed?.({status:failure.status,responseJSON:{error_message:failure.message}}));return promise;
    };
    api.DeleteFile=(name,group,done,failed)=>api.Delete(`files/${encodeURIComponent(group)}/${encodeURIComponent(name)}`,done,failed);
    return Object.freeze(api);
  }
  const legacyIds = { receiptscanner: 'receipt-scanner', nerdcore: 'core' };
  function legacyFacade(addonId) {
    return Object.freeze({
      version: '1.0.0', registerAddon: meta => register({ ...meta, id: legacyIds[meta.id] || meta.id }),
      getSettings: () => ({ ...configuration.settings }), saveSettings: next => wire('/settings', { method: 'PUT', data: { data: next } }),
      getUiLanguage: () => configuration.settings?.uiLanguage || 'fr', getCurrencySymbol: () => configuration.settings?.currencySymbol || '€',
      refreshCurrencySymbol: () => configuration.settings?.currencySymbol || '€', isLegacySettingsDisabled: () => false,
      isServerOffloadEnabled:()=>true,runtimeBarcodeSearch:(productName,provider,options={})=>scope(addonId).runtime('barcode.search',{productName,provider,pageSize:options.pageSize||24}),
      getUpdateApiBase: () => `${windowObject.location.origin}/__nerdcore_update`,
      getUpdateToken: () => '', setUpdateToken: () => {},
      runUpdateCheck: params => scope('grocyste').runtime('addons.check', params), runUpdateInstall: params => scope('grocyste').runtime('addons.install', params),
      openSettingsPage: section => { windowObject.location.href = `${windowObject.Grocy?.BaseUrl || ''}/stocksettings?${encodeURIComponent(section || 'grocyste')}=1`; },
      t: key => key,
    });
  }
  function compatWindow(addonId) {
    const grocy = Object.create(windowObject.Grocy || {}); grocy.Api = compatApi(addonId);
    const fetch = compatFetch(addonId), facade = legacyFacade(addonId);
    return new Proxy(windowObject, { get(target, key) {
      if (key === 'Grocy') return grocy;
      if (key === 'fetch') return fetch;
      if (key === 'NerdCore') return facade;
      const value = Reflect.get(target, key, target);
      return typeof value === 'function' && !/^[A-Z]/.test(String(key)) ? value.bind(target) : value;
    }, set(target, key, value) { return Reflect.set(target, key, value, target); } });
  }
  function compatStorage(addonId) {
    const local = windowObject.localStorage, cache = compatCaches.get(addonId)||new Map();compatCaches.set(addonId,cache); let queue = Promise.resolve();
    const client = scope(addonId);
    const sanitize=value=>{let safe=String(value);try{const parsed=JSON.parse(safe);for(const config of Object.values(parsed?.providers||{}))if(config.apiKey&&!String(config.apiKey).startsWith('grocyste-credential:'))config.apiKey='';safe=JSON.stringify(parsed);}catch{}return safe;};
    const synchronize = () => {
      const data = Object.fromEntries([...cache].map(([key,value])=>[key,sanitize(value)]));
      queue = queue.catch(() => {}).then(() => client.putStorage('settings', data)).catch(() => {});
    };
    return {
      getItem(key) { if (/nerdcore.*token/i.test(key)) return null; if (!cache.has(key)) cache.set(key, local.getItem(key)); return cache.get(key) ?? null; },
      setItem(key, value) {
        if (/nerdcore.*token/i.test(key)) return;
        // Provider secrets are migrated by migrateLegacyCredentials, never sent in settings snapshots.
        const safe = sanitize(value);
        cache.set(key, safe); local.setItem(key, String(value));
        migrateLegacyCredentials(addonId).then(() => { cache.set(key, sanitize(local.getItem(key))); synchronize(); }).catch(() => {});
      },
      removeItem(key) { cache.delete(key); local.removeItem(key); synchronize(); },
      key(index) { return local.key(index); }, get length() { return local.length; },
    };
  }
  async function prepareAddon(addonId) {
    await migrateLegacyCredentials(addonId);
    try {const saved=await scope(addonId).getStorage('settings');const values=saved.data;if(values&&typeof values==='object'&&!Array.isArray(values))compatCaches.set(addonId,new Map(Object.entries(values)));}catch{}
  }
  async function loadImage(addonId,image,url) {
    if(!url){image.removeAttribute?.('src');return;}
    const target=new URL(url,windowObject.location.href);
    if(['data:','blob:'].includes(target.protocol)){image.src=url;return;}
    const response=await compatFetch(addonId)(target.href);if(!response.ok)throw error('Image indisponible',response.status);
    const value=windowObject.URL.createObjectURL(await response.blob());
    const previous=image.dataset?.grocysteImage;if(previous)windowObject.URL.revokeObjectURL(previous);
    if(image.dataset)image.dataset.grocysteImage=value;image.src=value;
  }
  function observeImages(addonId,documentObject=windowObject.document) {
    if(!documentObject||!windowObject.MutationObserver)return;
    const selector=`img[data-grocyste-addon="${addonId}"][data-grocyste-src]`;
    const hydrate=node=>{for(const image of [ ...(node.matches?.(selector)?[node]:[]),...(node.querySelectorAll?.(selector)||[])]){const url=image.getAttribute('data-grocyste-src');image.removeAttribute('data-grocyste-src');loadImage(addonId,image,url).catch(()=>{image.dataset.grocysteImageError='1';});}};
    const observer=new windowObject.MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)hydrate(node);});
    observer.observe(documentObject.documentElement,{childList:true,subtree:true});hydrate(documentObject);return observer;
  }
  async function migrateLegacyCredentials(addonId) {
    const local = windowObject.localStorage, client = scope(addonId);
    for (let i = 0; i < local.length; i++) {
      const key = local.key(i); if (!key || !/ai.*settings/i.test(key)) continue;
      let parsed; try { parsed = JSON.parse(local.getItem(key)); } catch { continue; }
      let changed = false;
      for (const [provider, config] of Object.entries(parsed?.providers || {})) {
        if (!config?.apiKey || config.apiKey.startsWith('grocyste-credential:')) continue;
        try { config.apiKey = await client.credential(provider, config.apiKey); changed = true; } catch { /* Preserve the existing credential until a confirmed vault receipt. */ }
      }
      if (changed) local.setItem(key, JSON.stringify(parsed));
    }
    local.removeItem('grocy_nerdcore_update_token_v1');
  }
  function register(meta) {
    if (!meta || !validId(meta.id)) throw new TypeError('Métadonnées addon invalides');
    registered.set(meta.id, { ...meta }); windowObject.dispatchEvent(new windowObject.CustomEvent('grocyste:addon-registered', { detail: { id: meta.id } }));
    return scope(meta.id);
  }
  const assetUrl = (id, path) => { if (!validId(id) || !/^[a-zA-Z0-9_./-]+$/.test(path) || path.split('/').includes('..')) throw new TypeError('Chemin asset invalide'); return `${configuration.basePath || base.replace(/\/v1$/, '')}/assets/${id}/${path}`; };
  return Object.freeze({ version: '1.0.0', initialize, authenticate, scope, register, modules, compatFetch, compatApi, compatWindow, compatStorage, migrateLegacyCredentials, prepareAddon, loadImage, observeImages, assetUrl, tasks: async () => (await wire('/jobs')).jobs, settings: () => configuration.settings || {}, registered: () => [...registered.values()], configuration: () => configuration, session: () => session ? { user: session.user, isAdmin:session.isAdmin, serviceConfigured:session.serviceConfigured, capabilities: session.capabilities, addons: session.addons } : null, available: () => available, transport: wire });
}

function coreApiBase(windowObject, documentObject) {
  const source=documentObject.querySelector('script[data-grocyste-loader="1"]')?.src;
  if(source)try {
    const url=new URL(source,windowObject.location.href);
    if(url.origin===windowObject.location.origin&&/^(?:\/[A-Za-z0-9_-]+)+\/assets\/core\.js$/.test(url.pathname))return url.pathname.slice(0,-'/assets/core.js'.length)+'/v1';
  } catch { /* A foreign or malformed loader never chooses a transport origin. */ }
  return '/__grocyste/v1';
}

async function bootGrocyste(windowObject = window, documentObject = document) {
  if (windowObject.Grocyste) return windowObject.Grocyste;
  const sdk = createGrocysteSdk({ windowObject, base:coreApiBase(windowObject,documentObject) }); windowObject.Grocyste = sdk;
  if (!await sdk.initialize()) return sdk;
  const item = documentObject.createElement('a'); item.className = 'dropdown-item'; item.textContent = 'Grocyste — Assaisonnements'; item.href = `${(windowObject.Grocy?.BaseUrl || '').replace(/\/$/,'')}/stocksettings?grocyste=1`;
  const menu = documentObject.querySelector('#topnav-settings-dropdown .dropdown-menu, .dropdown-menu[aria-labelledby="topnav-settings-dropdown"], a[href$="/stocksettings"]')?.closest('.dropdown-menu');
  menu?.append(item);
  const session = sdk.session(), entries = Array.isArray(session.addons) ? session.addons : Object.entries(session.addons || {}).map(([id, value]) => ({ id, ...value }));
  const loaded = new Set(), pending = entries.filter(entry => entry.enabled !== false && entry.manifest?.entrypoints?.browser);
  while (pending.length) {
    const index = pending.findIndex(entry => Object.keys(entry.manifest.dependencies || {}).filter(id => id !== 'core').every(id => loaded.has(id)));
    if (index < 0) break;
    const entry = pending.splice(index, 1)[0];
    try {
      await sdk.prepareAddon(entry.id);
      if (entry.manifest.entrypoints.styles) { const style = documentObject.createElement('link'); style.rel = 'stylesheet'; style.href = sdk.assetUrl(entry.id, entry.manifest.entrypoints.styles); style.dataset.grocysteAddon = entry.id; documentObject.head.append(style); }
      await new Promise((resolve, reject) => { const script = documentObject.createElement('script'); script.src = sdk.assetUrl(entry.id, entry.manifest.entrypoints.browser); script.dataset.grocysteAddon = entry.id; script.onload = resolve; script.onerror = reject; documentObject.head.append(script); });
      loaded.add(entry.id);
    } catch { /* One broken addon does not prevent native Grocy or the other addons. */ }
  }
  if (new URL(windowObject.location.href).searchParams.get('grocyste') !== '1') return sdk;
  const host = documentObject.querySelector('.content-wrapper, main, .container-fluid') || documentObject.body;
  const panel = documentObject.createElement('section'); panel.className = 'grocyste-admin card m-3';
  const title = documentObject.createElement('h1'); title.textContent = 'Grocyste — Seasonings enabler';
  const description = documentObject.createElement('p'); description.textContent = 'Les assaisonnements installés utilisent les droits de votre compte Grocy. Les opérations d’administration sont réservées aux administrateurs.';
  const status = documentObject.createElement('p'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  panel.append(title, description, status);
  if(session.isAdmin&&!session.serviceConfigured) {
    const pair=documentObject.createElement('button');pair.type='button';pair.className='btn btn-primary m-1';pair.textContent='Relier cette instance Grocy';
    pair.onclick=async()=>{pair.disabled=true;status.textContent='Association en cours…';try{await sdk.scope('grocyste').runtime('core.pair');status.textContent='Instance associée. Rechargez la page pour charger les assaisonnements.';}catch(error){status.textContent=error.message;}finally{pair.disabled=false;}};panel.append(pair);
  }
  for (const entry of entries) {
    const row = documentObject.createElement('div'); row.className = 'grocyste-addon-row';
    const label = documentObject.createElement('strong'); label.textContent = `${entry.manifest?.name || entry.id} · ${entry.version || ''} · ${entry.enabled === false ? 'désactivé' : loaded.has(entry.id) ? 'chargé' : 'disponible'}`; row.append(label);
    const admin = session.isAdmin || session.capabilities?.includes?.('addons.manage') || session.capabilities?.includes?.('ADMIN');
    if (admin) for (const [operation, text] of [['addons.check', 'Vérifier'], ['addons.install', 'Mettre à jour'], ['addons.disable', 'Désactiver'], ['addons.rollback', 'Revenir à la version précédente'], ['addons.uninstall','Désinstaller']]) {
      const button = documentObject.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm m-1'; button.textContent = text;
      button.onclick = async () => { button.disabled = true; status.textContent = 'Opération en cours…'; try { const catalog=sdk.configuration().catalog;const available=(Array.isArray(catalog?.addons)?catalog.addons:[]).find(addon=>addon.id===entry.id&&addon.tested);const result = await sdk.scope('grocyste').runtime(operation, { addonId: entry.id, version: operation==='addons.install'?available?.version||entry.version:entry.version }); status.textContent = result.jobId ? `Opération enregistrée : ${result.jobId}` : 'Opération terminée. Rechargez la page pour actualiser les assaisonnements.'; } catch (failure) { status.textContent = failure.message; } finally { button.disabled = false; } }; row.append(button);
    }
    panel.append(row);
  }
  if(session.isAdmin)for(const addon of sdk.configuration().catalog?.addons||[]) {
    if(!addon.tested||entries.some(entry=>entry.id===addon.id))continue;
    const row=documentObject.createElement('div'),label=documentObject.createElement('strong'),button=documentObject.createElement('button');label.textContent=`${addon.name} · ${addon.version}`;button.type='button';button.className='btn btn-outline-primary btn-sm m-1';button.textContent='Installer';
    button.onclick=async()=>{button.disabled=true;try{await sdk.scope('grocyste').runtime('addons.install',{addonId:addon.id,version:addon.version});status.textContent='Installation enregistrée. Rechargez la page après sa validation.';}catch(error){status.textContent=error.message;}finally{button.disabled=false;}};row.append(label,button);panel.append(row);
  }
  host.prepend(panel); return sdk;
}

const start=()=>bootGrocyste(window,document).catch(()=>{});if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
