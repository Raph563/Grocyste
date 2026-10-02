import { readFile, writeFile } from 'node:fs/promises';
import { Script } from 'node:vm';
const code = (await Promise.all(['sdk.mjs', 'admin.mjs'].map(path => readFile(new URL(path, import.meta.url), 'utf8')))).join('\n').replace(/^export /gm, '');
const output = `(()=>{'use strict';\n${code}\nconst start=()=>bootGrocyste(window,document).catch(()=>{});if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();\n})();\n`;
new Script(output); await writeFile(new URL('core.js', import.meta.url), output);
