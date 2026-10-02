export function coreApiBase(windowObject, documentObject) {
  const source=documentObject.querySelector('script[data-grocyste-loader="1"]')?.src;
  if(source)try {
    const url=new URL(source,windowObject.location.href);
    if(url.origin===windowObject.location.origin&&/^(?:\/[A-Za-z0-9_-]+)+\/assets\/core\.js$/.test(url.pathname))return url.pathname.slice(0,-'/assets/core.js'.length)+'/v1';
  } catch { /* A foreign or malformed loader never chooses a transport origin. */ }
  return '/__grocyste/v1';
}

export async function bootGrocyste(windowObject = window, documentObject = document) {
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
