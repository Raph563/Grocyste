import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createGrocysteSdk } from './sdk.mjs';
import { coreApiBase } from './admin.mjs';

function fixture({ refuse = false, refuseVault = false } = {}) {
  const calls = [], memory = new Map();
  const windowObject = { crypto: { randomUUID }, AbortSignal, location: { href: 'https://grocy.test/recipes', origin: 'https://grocy.test' }, Grocy: { Api: { native: true }, BaseUrl: 'https://grocy.test' }, fetch() {}, dispatchEvent() {}, CustomEvent: class { constructor(name, details) { Object.assign(this, { name, ...details }); } }, btoa: value => Buffer.from(value, 'binary').toString('base64'), atob: value => Buffer.from(value, 'base64').toString('binary'), localStorage: { getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value), removeItem: key => memory.delete(key), key: index => [...memory.keys()][index], get length() { return memory.size; } } };
  const fetchImpl = async (url, options) => {
    calls.push({ url, ...options, parsed: options.body ? JSON.parse(options.body) : null });
    if (refuse) return new Response(JSON.stringify({ ok: false, error: 'Absent' }), { status: 404 });
    if (url.endsWith('/public-config')) return Response.json({ ok: true, basePath: '/__grocyste' });
    if (url.endsWith('/auth/session')) return Response.json({ ok: true, csrfToken: 'csrf-fixture', user: { id: 4 }, capabilities: [], addons: [],instanceConfig:{sharedTimerEntityId:37},settings:{uiLanguage:'fr'} });
    if (url.endsWith('/grocy/request')) return options.body.includes('"raw":true') ? Response.json({ ok: true, status: 200, body: 'eyJpZCI6MX0=', bodyEncoding: 'base64', contentType: 'application/json' }) : Response.json({ ok: true, status: 200, data: [{ id: 1 }] });
    if (url.includes('/storage/')) return Response.json({ ok: true, data: {}, revision: 2 });
    if (url.includes('/events?')) return Response.json({ok:true,events:[{id:12,namespace:'budgets',kind:'storage.changed',data:{key:'prices',revision:3}}]});
    if (url.endsWith('/jobs')) return Response.json({ok:true,jobs:[{key:'synthetic-lost-write',state:'needs-reconciliation',status:null}]});
    if(refuseVault&&JSON.parse(options.body||'{}').operation==='credential.store')return Response.json({ok:false,error:'Coffre refusé'},{status:403});
    return Response.json({ ok: true, status: 200, body: '{"ok":true}', contentType: 'application/json' });
  };
  return { sdk: createGrocysteSdk({ windowObject, fetchImpl }), calls, windowObject, memory };
}
test('absence du core laisse Grocy natif et ses transports intacts', async () => {
  const { sdk, windowObject } = fixture({ refuse: true }), native = windowObject.Grocy.Api;
  assert.equal(await sdk.initialize(), false); assert.equal(sdk.available(), false); assert.equal(windowObject.Grocy.Api, native);
});

test('le chargeur de même origine détermine le préfixe CORE y compris sous Grocy /grocy',async()=>{
 const {windowObject,calls}=fixture();windowObject.location.href='https://grocy.test/grocy/recipes';
 const documentObject={querySelector:()=>({src:'https://grocy.test/grocy/__grocyste/assets/core.js'})};
 const base=coreApiBase(windowObject,documentObject);assert.equal(base,'/grocy/__grocyste/v1');
 const sdk=createGrocysteSdk({windowObject,base,fetchImpl:async(url,options)=>{calls.push({url,options});return url.endsWith('/public-config')?Response.json({ok:true,basePath:'/grocy/__grocyste'}):Response.json({ok:true,csrfToken:'fixture',user:{id:1},addons:[]});}});
 assert.equal(await sdk.initialize(),true);assert.ok(calls.every(call=>call.url.startsWith('/grocy/__grocyste/v1/')));
 assert.equal(sdk.assetUrl('budgets','dist/addon.js'),'/grocy/__grocyste/assets/budgets/dist/addon.js');
});

test('un chargeur étranger ou traversant ne choisit jamais la destination SDK',()=>{
 const {windowObject}=fixture();
 for(const source of ['https://other.test/custom/assets/core.js','https://grocy.test/__grocyste%2f..%2fother/assets/core.js','https://grocy.test/assets/core.js'])assert.equal(coreApiBase(windowObject,{querySelector:()=>({src:source})}),'/__grocyste/v1');
 assert.equal(coreApiBase(windowObject,{querySelector:()=>null}),'/__grocyste/v1');
});
test('configuration privée chargée seulement après session et coffre refusé ne fuit pas via settings',async()=>{
  const {sdk,memory,calls}=fixture({refuseVault:true});await sdk.initialize();assert.equal(sdk.configuration().instanceConfig.sharedTimerEntityId,37);
  const storage=sdk.compatStorage('producthelper');storage.setItem('grocy_ai_settings',JSON.stringify({providers:{openai:{apiKey:'preserved-secret-fixture'}}}));
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(JSON.parse(memory.get('grocy_ai_settings')).providers.openai.apiKey,'preserved-secret-fixture');
  const snapshots=calls.filter(call=>call.url.includes('/storage/')&&call.method==='PUT');assert.ok(snapshots.length);assert.doesNotMatch(JSON.stringify(snapshots),/preserved-secret-fixture/);
});
test('UploadFile utilise transport binaire core au lieu de l’API native',async()=>{
  const {sdk,calls,windowObject}=fixture();await sdk.initialize();const file=new Blob(['image-fixture'],{type:'image/png'});await sdk.compatApi('producthelper').UploadFile(file,'productpictures','dGVzdC5wbmc=');
  const payload=calls.at(-1).parsed;assert.equal(payload.bodyEncoding,'base64');assert.equal(payload.contentType,'image/png');assert.equal(payload.data,Buffer.from('image-fixture').toString('base64'));assert.equal(windowObject.Grocy.Api.native,true);
});
test('proxy scoped et cookies, CSRF, idempotence sans clé privilégiée', async () => {
  const { sdk, calls, windowObject } = fixture(); await sdk.initialize();
  assert.deepEqual(await sdk.scope('budgets').request('GET', 'objects/products'), [{ id: 1 }]);
  const call = calls.at(-1); assert.equal(call.parsed.path, 'api/objects/products'); assert.equal(call.parsed.addonId, 'budgets');
  assert.equal(call.headers.get('X-Grocyste-CSRF'), 'csrf-fixture'); assert.ok(call.headers.get('Idempotency-Key')); assert.equal(call.headers.has('GROCY-API-KEY'), false);
  assert.equal(call.credentials, 'same-origin'); assert.equal(windowObject.Grocy.Api.native, true);
});
test('fetch compat enlève les jetons legacy et reconstruit la réponse binaire', async () => {
  const { sdk, calls } = fixture(); await sdk.initialize();
  const response = await sdk.compatFetch('producthelper')('/api/objects/products', { headers: { 'GROCY-API-KEY': 'DO-NOT-FORWARD', 'X-NerdCore-Token': 'OLD-ADMIN' } });
  assert.deepEqual(await response.json(), { id: 1 }); assert.doesNotMatch(JSON.stringify(calls), /DO-NOT-FORWARD|OLD-ADMIN/);
  assert.equal(sdk.legacyFacade?.getUpdateToken, undefined);
});
test('mutation settings compare la révision et refuse les chemins assets traversants', async () => {
  const { sdk, calls } = fixture(); await sdk.initialize(); await sdk.scope('budgets').putStorage('settings', { language: 'fr' });
  assert.equal(calls.at(-1).headers.get('If-Match'), '2'); assert.throws(() => sdk.assetUrl('budgets', '../secret'), /invalide/);
  assert.equal(sdk.assetUrl('budgets', 'dist/addon.js'), '/__grocyste/assets/budgets/dist/addon.js');
});
test('une clé fournisseur ne quitte le navigateur que vers le coffre confirmé', async () => {
  const { sdk, memory, calls } = fixture(); memory.set('grocy_dash_ai_settings_v1', JSON.stringify({ providers: { openai: { apiKey: 'provider-secret-fixture', model: 'fixture' } } }));
  await sdk.initialize(); await sdk.migrateLegacyCredentials('producthelper');
  assert.equal(JSON.parse(memory.get('grocy_dash_ai_settings_v1')).providers.openai.apiKey, 'grocyste-credential:openai');
  const vault = calls.find(call => call.parsed?.operation === 'credential.store'); assert.equal(vault.parsed.params.secret, 'provider-secret-fixture');
  const response = await sdk.compatFetch('producthelper')('https://api.openai.com/v1/chat/completions', { method: 'POST', headers: { Authorization: 'Bearer grocyste-credential:openai' }, body: '{}' });
  assert.equal(response.status, 200); assert.equal(calls.at(-1).parsed.params.headers.Authorization, undefined);
});
test('événements scoped et tâches incertaines sont consultables sans répéter une mutation', async () => {
  const {sdk,calls}=fixture();await sdk.initialize();
  const events=await sdk.scope('budgets').events(11);assert.equal(events[0].id,12);assert.match(calls.at(-1).url,/addonId=budgets&after=11$/);
  assert.equal((await sdk.tasks())[0].state,'needs-reconciliation');assert.equal(calls.at(-1).method,'GET');
  assert.equal(sdk.settings().uiLanguage,'fr');await assert.rejects(()=>sdk.scope('budgets').events(-1));
});
