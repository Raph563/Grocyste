// SPDX-License-Identifier: GPL-3.0-or-later
import {readFile,lstat,readdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve, sep} from 'node:path';
import {pathToFileURL} from 'node:url';
const packages = resolve(process.env.PACKAGES_DIR || '/packages');
const registry = JSON.parse(await readFile(resolve(packages, 'current.json'), 'utf8'));
const addon = registry.addons?.['shared-timers'];
if (!addon?.enabled || !/^\d+\.\d+\.\d+$/u.test(addon.version)) throw new Error('SharedTimers vérifié et actif requis');
const entry = addon.manifest?.entrypoints?.runtime;
if (typeof entry !== 'string' || !addon.manifest.files?.[entry] || entry.includes('..') || entry.includes('\\')) throw new Error('Entrée runtime invalide');
const directory = resolve(packages, 'installed/shared-timers', addon.version);
const modulePath = resolve(directory, entry);
if (!modulePath.startsWith(directory + sep)) throw new Error('Chemin runtime invalide');
const observed=new Set();
async function verifyDirectory(folder,prefix='') {
  for(const name of await readdir(folder)) {
    const relative=prefix+name,path=resolve(folder,name),info=await lstat(path);
    if(info.isSymbolicLink())throw new Error('Lien interdit dans le paquet runtime');
    if(info.isDirectory()){await verifyDirectory(path,relative+'/');continue;}
    if(!info.isFile())throw new Error('Fichier runtime irrégulier');
    if(relative==='manifest.json'||relative==='manifest.sig')continue;
    const expected=addon.manifest.files[relative];
    if(!expected||info.size!==expected.size||info.size>16*1024*1024)throw new Error('Fichier runtime inattendu');
    const bytes=await readFile(path);
    if(createHash('sha256').update(bytes).digest('hex')!==expected.sha256)throw new Error('Empreinte runtime divergente');
    observed.add(relative);
  }
}
await verifyDirectory(directory);
if(Object.keys(addon.manifest.files).some(path=>!observed.has(path)))throw new Error('Fichier runtime manquant');
const {createLiveServer} = await import(pathToFileURL(modulePath));
if (!process.env.GROCY_URL || !process.env.PUBLIC_ORIGIN) throw new Error('Configuration instance explicite requise');
const configuration=JSON.parse(await readFile(process.env.INSTANCE_CONFIG_FILE||'/instance/instance.json','utf8'));
const server = await createLiveServer({grocyUrl:process.env.GROCY_URL,
  origin:process.env.PUBLIC_ORIGIN,stateDirectory:process.env.LIVE_STATE_DIRECTORY || '/state',
  packageMeasures:Array.isArray(configuration.recipePackageMeasures)?configuration.recipePackageMeasures:[]});
server.listen(Number(process.env.PORT || 8093), '0.0.0.0');
for (const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>server.close(()=>process.exit(0)));
